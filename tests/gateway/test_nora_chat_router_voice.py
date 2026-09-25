# //// Neoffice — added file (no upstream equivalent): a question asked aloud is answered in a
# //// form speech synthesis can say.
"""The voice clients (the NORA Live page and the quick chat's live widget) read a worker's
reply through speech synthesis and keep only its prose sentences: a table or a list is shown,
never spoken. A worker that opened on one left nothing, or only a preamble, to say before the
answer (#759). The router now tells the worker, for a voice turn only, to start with the answer
in one or two sentences."""
import sys
import types
from types import SimpleNamespace

import pytest

from gateway import nora_chat_router as router


# ── which turns are voice turns ──────────────────────────────────────────────────────

@pytest.mark.parametrize("page", (
    {"source": "nora-console-voice", "pole_hint": "compta", "tts_active": False},
    {"source": "nora-console-voice"},
    {"source": "nora-quick-voice"},
    {"source": "nora-live-widget"},       # the widget's own tag when its page names none
    {"source": " nora-quick-voice "},
))
def test_a_voice_turn_gets_the_hint(page):
    assert router.voice_answer_hint(page) == router.VOICE_ANSWER_HINT


@pytest.mark.parametrize("page", (
    {"source": "nora-console"},           # typed on the NORA Live page: shown, not spoken
    {"source": "desk"},
    {"doctype": "Customer", "name": "CUST-0001"},
    {"source": None},
    {},
    None,
    "nora-console-voice",                 # not a dict: never trusted
))
def test_any_other_turn_does_not(page):
    assert router.voice_answer_hint(page) is None


def test_the_hint_asks_for_the_answer_first_and_no_markup():
    hint = router.VOICE_ANSWER_HINT
    assert hint.startswith("[") and hint.endswith("]") and "\n" not in hint
    assert "asked aloud" in hint and "speech synthesis" in hint
    assert "one or two short sentences" in hint and "no table, list or markdown before it" in hint


# ── the hint reaches the worker, and only for a voice turn ────────────────────────────

def _fake_kanban(monkeypatch):
    """Intercept the task the router creates; returns the list of created tasks."""
    created = []
    kanban_db = types.SimpleNamespace(create_task=lambda conn, **kw: created.append(kw) or "t_test")
    connect = types.SimpleNamespace(connect=lambda board=None: SimpleNamespace(close=lambda: None))
    notify = types.SimpleNamespace(add_notify_sub=lambda *a, **kw: None)
    pkg = sys.modules.get("hermes_cli") or types.ModuleType("hermes_cli")
    monkeypatch.setitem(sys.modules, "hermes_cli", pkg)
    for name, mod in (("kanban_db", kanban_db), ("kanban_db_connect", connect), ("kanban_db_notify", notify)):
        monkeypatch.setattr(pkg, name, mod, raising=False)
        monkeypatch.setitem(sys.modules, f"hermes_cli.{name}", mod)
    return created


def _llm_says(pole):
    return lambda **_kw: SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=pole))])


def _route(message, conversation_id, page_context=None):
    return router.route_chat_message(
        message=message, session_chat_id="webhook:nora_chat:t", conversation_id=conversation_id,
        thread_id=None, user_id="u", notifier_profile="default", idempotency_key=f"k-{conversation_id}-{message}",
        call_llm_fn=_llm_says("compta"), main_runtime=None, page_context=page_context)


@pytest.mark.parametrize("source", ("nora-console-voice", "nora-quick-voice"))
def test_a_voice_task_ends_on_the_hint(monkeypatch, source):
    created = _fake_kanban(monkeypatch)
    _route("Quel est le total des factures impayées de ce mois ?", f"conv-voice-{source}",
           page_context={"source": source, "pole_hint": "compta", "tts_active": False})
    assert created, "the request did not reach a pole"
    body = created[-1]["body"]
    assert body.endswith(router.VOICE_ANSWER_HINT), "the reply-form directives close the body"
    assert "[Reply to the user in" in body, "the language directive is still there"
    assert body.index("[Reply to the user in") < body.index(router.VOICE_ANSWER_HINT)
    assert body.count(router.VOICE_ANSWER_HINT) == 1


@pytest.mark.parametrize("page", (
    None,
    {"source": "nora-console"},
    {"source": "desk", "doctype": "Customer", "name": "CUST-0001", "title": "Boulangerie du Lac"},
))
def test_a_typed_task_has_no_voice_hint(monkeypatch, page):
    created = _fake_kanban(monkeypatch)
    _route("Quel est le total des factures impayées de ce mois ?", f"conv-typed-{id(page)}", page_context=page)
    assert created, "the request did not reach a pole"
    assert router.VOICE_ANSWER_HINT not in created[-1]["body"]


def test_voice_on_a_document_page_keeps_the_anchor(monkeypatch):
    created = _fake_kanban(monkeypatch)
    _route("Combien ce client nous doit-il ?", "conv-voice-page",
           page_context={"source": "nora-quick-voice", "doctype": "Customer", "name": "CUST-0001",
                         "title": "Boulangerie du Lac"})
    body = created[-1]["body"]
    assert "the Customer « Boulangerie du Lac » (CUST-0001)" in body
    assert body.endswith(router.VOICE_ANSWER_HINT)
