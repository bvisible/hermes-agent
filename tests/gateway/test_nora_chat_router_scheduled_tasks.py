# //// Neoffice — added file (no upstream equivalent): the person's scheduled tasks are the orchestrator's (09.10).
"""« Mets en pause la tâche des devis ouverts » is the orchestrator's, never the pole its words name.

On the chat she holds nora_list_tasks, nora_pause_task and nora_delete_task, and no pole and no code route
does. In the Quick Chat on 09.10, the pause went to ventes (the word « devis »), which has no such tool, wrote
the internal tool's name to the person and sent them to their administrator; « Supprime-la » and « Oui »
followed it there. A question nora refuses to turn into a task (reason « question ») comes to her too.
"""
import io
import json
import sys
import types
from types import SimpleNamespace

import pytest

from gateway import nora_chat_router as R

_CB = "https://erp.example.test/api/method/nora.api.v2.hermes_callback.deliver"
_EXTRA = {"callback_url": _CB, "callback_token": "tok", "conversation_id": "conv-tasks"}


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
    kanban_db = types.SimpleNamespace(create_task=lambda conn, **kw: created.append(kw) or "t_tasks")
    connect = types.SimpleNamespace(connect=lambda board=None: SimpleNamespace(close=lambda: None))
    notify = types.SimpleNamespace(add_notify_sub=lambda *a, **kw: None)
    pkg = sys.modules.get("hermes_cli") or types.ModuleType("hermes_cli")
    monkeypatch.setitem(sys.modules, "hermes_cli", pkg)
    for name, mod in (("kanban_db", kanban_db), ("kanban_db_connect", connect), ("kanban_db_notify", notify)):
        monkeypatch.setattr(pkg, name, mod, raising=False)
        monkeypatch.setitem(sys.modules, f"hermes_cli.{name}", mod)
    return created


def _route(monkeypatch, message, verdict, *, recurrent=None):
    """Route *message* with the classifier answering *verdict*; returns (decision, classify calls)."""
    import urllib.request

    calls = []

    def fake_classify(message, **kw):
        calls.append(kw)
        return verdict

    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda req, timeout=None: _Resp(io.BytesIO(json.dumps({"message": "ok"}).encode())),
    )
    monkeypatch.setattr(R, "_asks_fast_answer", lambda *a, **k: False)
    monkeypatch.setattr(R, "classify", fake_classify)
    if recurrent is not None:
        monkeypatch.setattr(R, "_route_recurrent", lambda *a, **k: dict(recurrent))
    decision = R.route_chat_message(
        message=message, session_chat_id="webhook:nora_chat:test", conversation_id="conv-tasks",
        thread_id=None, user_id="u", notifier_profile="default", idempotency_key="k",
        call_llm_fn=lambda **_kw: None, main_runtime=None, deliver_extra=dict(_EXTRA),
        chat_user="staff@example.test", language="fr", nora_spoke=True,
    )
    return decision, calls


def test_pausing_a_task_whose_words_name_a_pole_stays_with_the_orchestrator(monkeypatch):
    created = _fake_kanban(monkeypatch)
    decision, calls = _route(monkeypatch, "Mets en pause la tâche des devis ouverts.", "ventes")

    assert decision["routed"] is False and decision["category"] == "DIRECT", decision
    assert created == [] and calls == [], "no pole, and the classifier is not asked"
    assert "nora_pause_task" in decision["agent_hint"]


def test_the_follow_ups_of_a_task_turn_stay_with_her(monkeypatch):
    created = _fake_kanban(monkeypatch)
    _route(monkeypatch, "Mets en pause la tâche des devis ouverts.", "ventes")
    for message in ("Supprime-la.", "Oui."):
        decision, calls = _route(monkeypatch, message, "ventes")
        assert decision["category"] == "DIRECT" and calls == [], (message, decision)
    assert created == []


def test_a_question_nora_refuses_to_schedule_comes_to_her_with_the_task_tools(monkeypatch):
    created = _fake_kanban(monkeypatch)
    refused = {"routed": False, "category": "recurrent", "ack": None, "task_id": None, "reason": "question"}
    decision, _ = _route(monkeypatch, "Quelles sont mes tâches programmées pour les devis ?", "recurrent",
                         recurrent=refused)

    assert decision["category"] == "DIRECT" and created == [], "never the ventes pole the words name"
    assert "nora_list_tasks" in decision["agent_hint"]
    assert R._LAST_ROUTE["conv-tasks"]["tasks"] is True


def test_another_subject_after_a_task_turn_is_routed_as_usual(monkeypatch):
    created = _fake_kanban(monkeypatch)
    _route(monkeypatch, "Mets en pause la tâche des devis ouverts.", "ventes")
    decision, calls = _route(monkeypatch, "Combien de devis ouverts ai-je ?", "ventes")
    assert decision["category"] == "ventes" and len(calls) == 1 and created[-1]["assignee"] == "ventes"


@pytest.mark.parametrize("message, prior, expected", [
    ("Mets en pause la tâche des devis ouverts.", None, True),
    ("Supprime ma relève du matin", None, True),
    ("Désactive la tâche programmée du lundi", None, True),
    ("Pause the scheduled task for open quotes", None, True),
    ("Lösche die geplante Aufgabe", None, True),
    ("Supprime la ligne 2 du devis DEVIS-2026-00012", None, False),
    ("Arrête la tâche du chantier Dupont", None, False),      # a job's task, without a turn about the tasks
    ("Supprime-la.", {"tasks": True}, True),
    ("Oui.", {"tasks": True}, True),
    ("non, garde-la", {"tasks": True}, True),
    ("Combien de devis ouverts ?", {"tasks": True}, False),
    ("Supprime-la.", {"pole": "ventes"}, False),
    ("ok crée un rappel pour les impayés", {"tasks": True}, False),   # a yes that opens a new request
    ("Supprime la facture FA-2026-00012 et envoie un avoir au client", {"tasks": True}, False),
])
def test_what_manages_a_scheduled_task(message, prior, expected):
    assert R._manages_scheduled_tasks(message, prior) is expected
