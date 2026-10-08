# //// Neoffice — added file (no upstream equivalent): « où je trouve … ? » is never left to DIRECT (09.10).
"""« Quel rapport me permet de suivre ce que mes clients me doivent ? » reaches a pole, never the orchestrator.

The classifier may call such a question 'direct' (its answer fits in one sentence), but on the chat the
orchestrator holds no map of Neoffice: on 08.10 it called a tool it does not have, searched its doctrine wiki
five times with the same words and answered that it could not finish, where the compta pole had named the
receivables report every night before. A 'direct' verdict on it is asked again with 'direct' excluded.
"""
import io
import json
import sys
import types
from types import SimpleNamespace

import pytest

from gateway import nora_chat_router as R

_CB = "https://erp.example.test/api/method/nora.api.v2.hermes_callback.deliver"
_EXTRA = {"callback_url": _CB, "callback_token": "tok", "conversation_id": "conv-where"}


@pytest.fixture(autouse=True)
def fresh_state():
    for d in (R._LAST_ROUTE, R._PENDING_SPACE, R._PENDING_NOTE, R._CONV_HISTORY, R._SPACE_ASKED):
        d.clear()
    yield
    for d in (R._LAST_ROUTE, R._PENDING_SPACE, R._PENDING_NOTE, R._CONV_HISTORY, R._SPACE_ASKED):
        d.clear()


class _Resp:
    def __init__(self, buf):
        self._buf = buf

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return self._buf.getvalue()


def _fake_kanban(monkeypatch):
    created = []
    kanban_db = types.SimpleNamespace(create_task=lambda conn, **kw: created.append(kw) or "t_where")
    connect = types.SimpleNamespace(connect=lambda board=None: SimpleNamespace(close=lambda: None))
    notify = types.SimpleNamespace(add_notify_sub=lambda *a, **kw: None)
    pkg = sys.modules.get("hermes_cli") or types.ModuleType("hermes_cli")
    monkeypatch.setitem(sys.modules, "hermes_cli", pkg)
    for name, mod in (("kanban_db", kanban_db), ("kanban_db_connect", connect), ("kanban_db_notify", notify)):
        monkeypatch.setattr(pkg, name, mod, raising=False)
        monkeypatch.setitem(sys.modules, f"hermes_cli.{name}", mod)
    return created


def _route(monkeypatch, message, verdicts):
    """Route *message* with the classifier answering *verdicts* in turn; returns (decision, calls)."""
    import urllib.request

    calls = []

    def fake_classify(message, **kw):
        calls.append(kw)
        return verdicts[len(calls) - 1]

    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda req, timeout=None: _Resp(io.BytesIO(json.dumps({"message": "ok"}).encode())),
    )
    monkeypatch.setattr(R, "_asks_fast_answer", lambda *a, **k: False)
    monkeypatch.setattr(R, "classify", fake_classify)
    decision = R.route_chat_message(
        message=message, session_chat_id="webhook:nora_chat:test", conversation_id="conv-where",
        thread_id=None, user_id="u", notifier_profile="default", idempotency_key="k",
        call_llm_fn=lambda **_kw: None, main_runtime=None, deliver_extra=dict(_EXTRA),
        chat_user="staff@example.test", language="fr", nora_spoke=True,
    )
    return decision, calls


def test_a_direct_verdict_is_asked_again_and_the_pole_of_the_subject_answers(monkeypatch):
    created = _fake_kanban(monkeypatch)
    message = "Quel rapport me permet de suivre ce que mes clients me doivent encore ?"
    decision, calls = _route(monkeypatch, message, ["DIRECT", "compta"])

    assert decision["routed"] is True and decision["category"] == "compta"
    assert created[-1]["assignee"] == "compta"
    assert [kw.get("exclude_direct", False) for kw in calls] == [False, True]


def test_support_answers_when_the_classifier_still_says_direct(monkeypatch):
    created = _fake_kanban(monkeypatch)
    decision, _ = _route(monkeypatch, "Où je trouve les réglages de l'imprimante ?", ["DIRECT", "DIRECT"])
    assert decision["category"] == "support" and created[-1]["assignee"] == "support"


def test_a_pole_chosen_at_once_is_kept_and_asked_once(monkeypatch):
    _fake_kanban(monkeypatch)
    decision, calls = _route(monkeypatch, "Dans quel menu sont les devis ?", ["ventes"])
    assert decision["category"] == "ventes" and len(calls) == 1


def test_another_direct_question_stays_direct(monkeypatch):
    created = _fake_kanban(monkeypatch)
    decision, calls = _route(monkeypatch, "Quelle langue parles-tu ?", ["DIRECT"])
    assert decision["routed"] is False and decision["category"] == "DIRECT"
    assert created == [] and not any(kw.get("exclude_direct") for kw in calls)


@pytest.mark.parametrize("message", [
    "Quel rapport me permet de suivre ce que mes clients me doivent encore ?",
    "quels rapports pour la TVA ?",
    "Où je trouve les fiches de paie ?",
    "où est-ce que je peux voir mes devis ?",
    "Ou se trouve le grand livre ?",
    "Dans quel menu sont les notes de frais ?",
    "Quel écran montre le stock ?",
    "Where can I find the aged receivables?",
    "Which report shows unpaid invoices?",
    "Wo finde ich die offenen Posten?",
    "Dove trovo le fatture?",
])
def test_where_to_find_questions_are_recognised(message):
    assert R._WHERE_TO_FIND_RE.search(message), message


@pytest.mark.parametrize("message", [
    "Quel est le montant de la facture FA-2026-00012 ?",
    "Quel client me doit le plus ?",
    "Combien de devis ouverts ?",
    "Bonjour, ça va ?",
    "Crée un devis pour Dupont ou Martin",
])
def test_other_questions_are_not(message):
    assert not R._WHERE_TO_FIND_RE.search(message), message


def test_asked_again_the_classifier_is_told_direct_is_excluded_and_rules_are_skipped():
    seen = []

    def call_llm_fn(**kw):
        seen.append(kw["messages"])
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="compta"))])

    # « est-ce que tu peux … » alone is a capability question for the rules (DIRECT, no LLM): asked again,
    # the rules are not consulted and the model is.
    verdict = R.classify(
        "Est-ce que tu peux me dire où je trouve le rapport des débiteurs ?",
        call_llm_fn=call_llm_fn, main_runtime=None, exclude_direct=True,
    )
    assert verdict == "compta"
    assert len(seen) == 1 and "'direct' est exclu" in seen[0][-1]["content"]
