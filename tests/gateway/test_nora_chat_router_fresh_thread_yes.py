# //// Neoffice — added file (no upstream equivalent): a yes in a thread where NORA has said nothing yet
# //// confirms nothing (#1065).
"""« Oui, vas-y. » that opens a thread confirms nothing.

The agent's session is kept per person, the film per thread. A fresh Quick Chat thread opened on « Oui,
vas-y. », and the orchestrator found in its session a proposal from another thread of the morning and handed
it to a pole. The router knows a thread is fresh when nora says NORA never replied in it (`nora_spoke`, read
from its chat log) and its own film agrees; then a bare yes is answered in code, and any other yes reaches the
orchestrator or the pole with a rule that forbids acting on another conversation.
"""
import io
import json
import sys
import types
from types import SimpleNamespace

import pytest

from gateway import nora_chat_router as R

_CB = "https://erp.example.test/api/method/nora.api.v2.hermes_callback.deliver"
_EXTRA = {"callback_url": _CB, "callback_token": "tok", "conversation_id": "conv-new"}

BARE_YES = ["oui", "Oui, vas-y.", "ok", "OK !", "D'accord", "d\u2019accord", "Oui oui", "Vas-y", "Bien sûr",
            "Oui merci", "yes", "Ja, gerne", "Sì, va bene"]


@pytest.fixture(autouse=True)
def fresh_state():
    for d in (R._LAST_ROUTE, R._PENDING_SPACE, R._PENDING_NOTE, R._CONV_HISTORY, R._SPACE_ASKED):
        d.clear()
    yield
    for d in (R._LAST_ROUTE, R._PENDING_SPACE, R._PENDING_NOTE, R._CONV_HISTORY, R._SPACE_ASKED):
        d.clear()


class _Http:
    """Stands in for urllib.request.urlopen; records every POST to the desk callback."""

    def __init__(self):
        self.calls = []

    def __call__(self, req, timeout=None):
        self.calls.append((req.full_url, json.loads(req.data.decode())))
        return _Resp(io.BytesIO(json.dumps({"message": "ok"}).encode()))


class _Resp:
    def __init__(self, buf):
        self._buf = buf

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return self._buf.getvalue()


def _no_llm(**_kw):
    raise AssertionError("a bare yes in a fresh thread is answered without the classifier")


def _classifier(verdict):
    return lambda **_kw: SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=verdict))])


def _route(monkeypatch, message, *, nora_spoke, call_llm_fn=_no_llm, language="fr"):
    import urllib.request

    http = _Http()
    monkeypatch.setattr(urllib.request, "urlopen", http)
    monkeypatch.setattr(R, "_asks_fast_answer", lambda *a, **k: False)
    decision = R.route_chat_message(
        message=message, session_chat_id="webhook:nora_chat:test", conversation_id="conv-new",
        thread_id=None, user_id="u", notifier_profile="default", idempotency_key="k",
        call_llm_fn=call_llm_fn, main_runtime=None, deliver_extra=dict(_EXTRA),
        chat_user="staff@example.test", language=language, page_context=None, nora_spoke=nora_spoke)
    return decision, http


def _fake_kanban(monkeypatch):
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


@pytest.mark.parametrize("message", BARE_YES)
def test_a_bare_yes_in_a_fresh_thread_is_answered_in_code(monkeypatch, message):
    decision, http = _route(monkeypatch, message, nora_spoke=False)
    assert decision["routed"] is True and decision["task_id"] is None
    assert decision["ack"] == R._NOTHING_PROPOSED["fr"]
    assert http.calls == [(_CB, {"conversation_id": "conv-new", "text": R._NOTHING_PROPOSED["fr"]})]
    assert R._CONV_HISTORY["conv-new"][-1].startswith("NORA: Je n'ai rien en attente")


@pytest.mark.parametrize("language, message", (("de", "Ja"), ("it", "Sì"), ("en", "Yes, sure")))
def test_the_answer_is_in_the_persons_language(monkeypatch, language, message):
    decision, _http = _route(monkeypatch, message, nora_spoke=False, language=language)
    assert decision["ack"] == R._NOTHING_PROPOSED[language]


def test_no_answer_text_asks_the_nightly_bench_to_say_yes_again():
    # The capability bench answers « Oui, vas-y. » to any « voulez-vous que je … »: a loop.
    assert all("voulez-vous que je" not in text.lower() for text in R._NOTHING_PROPOSED.values())


@pytest.mark.parametrize("nora_spoke", (True, None))
def test_a_yes_where_nora_spoke_or_may_have_spoken_is_routed_as_before(monkeypatch, nora_spoke):
    asked = []
    monkeypatch.setattr(R, "classify", lambda message, **kw: asked.append(message) or "DIRECT")
    decision, http = _route(monkeypatch, "Oui, vas-y.", nora_spoke=nora_spoke)
    assert asked == ["Oui, vas-y."]
    assert decision["routed"] is False and decision.get("agent_hint") is None
    assert http.calls == []


def test_this_gateways_film_wins_over_a_flag_that_says_fresh(monkeypatch):
    # NORA's reply was delivered but its chat-log row was lost: the film knows she spoke.
    R._CONV_HISTORY["conv-new"] = ["User: Combien de devis ouverts ?", "NORA: Vous avez 12 devis ouverts."]
    monkeypatch.setattr(R, "classify", lambda message, **kw: "DIRECT")
    decision, _http = _route(monkeypatch, "Oui", nora_spoke=False)
    assert decision["routed"] is False


def test_a_thank_you_alone_stays_small_talk(monkeypatch):
    decision, _http = _route(monkeypatch, "Merci", nora_spoke=False)
    assert decision["ack"] == R._CANNED_REPLIES["fr"]["thanks"]


def test_a_note_question_left_open_keeps_waiting_for_the_note(monkeypatch):
    R._PENDING_NOTE["conv-new"] = __import__("time").time()
    R._CONV_HISTORY["conv-new"] = ["User: Peux-tu me créer une note ?", "NORA: Bien sûr. Que voulez-vous que je note ?"]
    monkeypatch.setattr(R, "classify", lambda message, **kw: "DIRECT")
    decision, _http = _route(monkeypatch, "Oui", nora_spoke=False)
    assert decision.get("ack") != R._NOTHING_PROPOSED["fr"]
    assert "conv-new" in R._PENDING_NOTE


def test_a_yes_with_more_words_reaches_the_orchestrator_with_the_rule(monkeypatch):
    monkeypatch.setattr(R, "classify", lambda message, **kw: "DIRECT")
    decision, http = _route(monkeypatch, "Oui, fais-le maintenant pour ce client", nora_spoke=False)
    assert decision["routed"] is False and decision["agent_hint"] == R._FRESH_THREAD_HINT
    assert http.calls == []


def test_a_yes_with_more_words_sent_to_a_pole_carries_the_rule(monkeypatch):
    created = _fake_kanban(monkeypatch)
    decision, _http = _route(monkeypatch, "Ok, envoie-le au client", nora_spoke=False,
                             call_llm_fn=_classifier("ventes"))
    assert decision["routed"] is True and created[-1]["assignee"] == "ventes"
    assert created[-1]["body"].startswith(R._FRESH_THREAD_HINT)
    assert "Ok, envoie-le au client" in created[-1]["body"]


def test_a_question_in_a_fresh_thread_gets_no_rule(monkeypatch):
    monkeypatch.setattr(R, "classify", lambda message, **kw: "DIRECT")
    decision, _http = _route(monkeypatch, "Comment créer un devis ?", nora_spoke=False)
    assert decision.get("agent_hint") is None
