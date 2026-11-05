# //// Neoffice — added file (no upstream equivalent): the ack of a message that answers a desk (#2285).
"""« Je transmets votre demande à votre pôle Restaurant » is false on a « oui » the Restaurant desk asked for.

The desk is already talking to the person, nothing is passed on. The ack stays, because every client waits for one: it is
what they skip to reach the answer. So each line keeps an anchor of `nora/utils/routing_ack.py`: an ack no client recognises
is shown as the answer, and the real answer is never shown.
"""
import io
import json
import sys
import types
from types import SimpleNamespace

import pytest

from gateway import nora_chat_router as R

_EXTRA = {"callback_url": "https://erp.example.test/cb", "callback_token": "tok", "conversation_id": "conv"}
# Copied from nora/utils/routing_ack.py, the gateway's own anchors: a change there or here must be seen in both.
_ANCHORS = {
    "fr": "je reviens vers vous",
    "de": "ich melde mich gleich",
    "it": "torno subito da lei",
    "en": "i'll be right back with you",
}


@pytest.fixture(autouse=True)
def fresh_state():
    for d in (R._LAST_ROUTE, R._PENDING_SPACE, R._PENDING_NOTE, R._CONV_HISTORY, R._SPACE_ASKED):
        d.clear()
    yield
    for d in (R._LAST_ROUTE, R._PENDING_SPACE, R._PENDING_NOTE, R._CONV_HISTORY, R._SPACE_ASKED):
        d.clear()


def _classifier(verdict):
    return lambda **_kw: SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=verdict))])


def _route(monkeypatch, message, waiting, *, language="fr", verdict="ventes"):
    import urllib.request

    created = []
    kanban_db = types.SimpleNamespace(create_task=lambda conn, **kw: created.append(kw) or "t_test")
    connect = types.SimpleNamespace(connect=lambda board=None: SimpleNamespace(close=lambda: None))
    notify = types.SimpleNamespace(add_notify_sub=lambda *a, **kw: None)
    pkg = sys.modules.get("hermes_cli") or types.ModuleType("hermes_cli")
    monkeypatch.setitem(sys.modules, "hermes_cli", pkg)
    for name, mod in (("kanban_db", kanban_db), ("kanban_db_connect", connect), ("kanban_db_notify", notify)):
        monkeypatch.setattr(pkg, name, mod, raising=False)
        monkeypatch.setitem(sys.modules, f"hermes_cli.{name}", mod)
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: io.BytesIO(json.dumps({"message": "ok"}).encode()))
    monkeypatch.setattr(R, "_asks_fast_answer", lambda *a, **k: False)
    monkeypatch.setattr(R, "_question_waiting", lambda *a, **k: waiting)
    decision = R.route_chat_message(
        message=message, session_chat_id="webhook:nora_chat:test", conversation_id="conv",
        thread_id=None, user_id="u", notifier_profile="default", idempotency_key="k",
        call_llm_fn=_classifier(verdict), main_runtime=None, deliver_extra=dict(_EXTRA),
        chat_user="staff@example.test", language=language, page_context=None, nora_spoke=True)
    return decision, created


@pytest.mark.parametrize(
    "message, language, ack",
    [("oui", "fr", "C'est noté, je reviens vers vous très vite."),
     ("Non, laisse tomber", "fr", "C'est noté, je reviens vers vous très vite."),
     ("on facture à l'heure", "fr", "C'est noté, je reviens vers vous très vite."),
     ("ja", "de", "Verstanden — ich melde mich gleich."),
     ("sì", "it", "Ricevuto, torno subito da lei."),
     ("yes", "en", "Got it — I'll be right back with you.")],
)
def test_an_answer_to_a_desk_is_not_passed_on_to_that_desk(monkeypatch, message, language, ack):
    decision, created = _route(monkeypatch, message, "resto", language=language)
    assert created[0]["assignee"] == "resto"
    assert decision["ack"] == ack


def test_a_request_with_no_question_waiting_is_passed_on(monkeypatch):
    decision, created = _route(monkeypatch, "Fais une facture à Dupont", None)
    assert created[0]["assignee"] == "ventes"
    assert decision["ack"] == "Je transmets votre demande à votre pôle Ventes, je reviens vers vous très vite."


def test_a_new_request_for_another_desk_is_passed_on(monkeypatch):
    decision, created = _route(monkeypatch, "Fais une facture à Dupont", "resto")
    assert created[0]["assignee"] == "ventes"
    assert decision["ack"] == "Je transmets votre demande à votre pôle Ventes, je reviens vers vous très vite."


@pytest.mark.parametrize("language", sorted(_ANCHORS))
def test_every_ack_stays_one_the_clients_can_tell_from_an_answer(language):
    for ack in (R.build_ack("resto", language), R.build_ack("resto", language, answering=True)):
        assert _ANCHORS[language] in ack.lower().replace("’", "'"), ack
