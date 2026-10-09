# //// Neoffice — added file (no upstream equivalent): a reminder asked without its moment (09.10).
"""« Rappelle-moi d'appeler le fournisseur de carrelage »: NORA asks when, in code, and sets ONE reminder.

Without a moment the request was not a one-off reminder for the router: it reached the orchestrator, which
set it at a time it made up (today 09:00) after 35 s, and « Demain à 10h » that followed set a second one
(Quick Chat on the dev instance, 09.10). The moment is now asked in code and the answer completes the request
for nora's route_reminder. « Rappelle-moi le nom du client » asks for information, not for a reminder.
"""
import io
import json
import sys
import types
from types import SimpleNamespace

import pytest

from gateway import nora_chat_router as R

_CB = "https://erp.example.test/api/method/nora.api.v2.hermes_callback.deliver"
_EXTRA = {"callback_url": _CB, "callback_token": "tok", "conversation_id": "conv-remind"}


@pytest.fixture(autouse=True)
def fresh_state():
    for d in (R._LAST_ROUTE, R._PENDING_SPACE, R._PENDING_NOTE, R._CONV_HISTORY, R._SPACE_ASKED,
              R._PENDING_REMINDER):
        d.clear()
    yield
    for d in (R._LAST_ROUTE, R._PENDING_SPACE, R._PENDING_NOTE, R._CONV_HISTORY, R._SPACE_ASKED,
              R._PENDING_REMINDER):
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
    kanban_db = types.SimpleNamespace(create_task=lambda conn, **kw: created.append(kw) or "t_remind")
    connect = types.SimpleNamespace(connect=lambda board=None: SimpleNamespace(close=lambda: None))
    notify = types.SimpleNamespace(add_notify_sub=lambda *a, **kw: None)
    pkg = sys.modules.get("hermes_cli") or types.ModuleType("hermes_cli")
    monkeypatch.setitem(sys.modules, "hermes_cli", pkg)
    for name, mod in (("kanban_db", kanban_db), ("kanban_db_connect", connect), ("kanban_db_notify", notify)):
        monkeypatch.setattr(pkg, name, mod, raising=False)
        monkeypatch.setitem(sys.modules, f"hermes_cli.{name}", mod)
    return created


@pytest.fixture
def harness(monkeypatch):
    import urllib.request

    posted, classified, reminders = [], [], []

    def urlopen(req, timeout=None):
        posted.append(json.loads(req.data.decode()))
        return _Resp(io.BytesIO(json.dumps({"message": "ok"}).encode()))

    def fake_classify(message, **kw):
        classified.append(message)
        return "support"

    def fake_route_reminder(message, chat_user, deliver_extra):
        reminders.append(message)
        return {"routed": True, "category": "DIRECT", "ack": "C'est noté ✅", "task_id": None}

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(R, "_asks_fast_answer", lambda *a, **k: False)
    monkeypatch.setattr(R, "classify", fake_classify)
    monkeypatch.setattr(R, "_route_reminder", fake_route_reminder)
    _fake_kanban(monkeypatch)

    def route(message):
        return R.route_chat_message(
            message=message, session_chat_id="webhook:nora_chat:test", conversation_id="conv-remind",
            thread_id=None, user_id="u", notifier_profile="default", idempotency_key="k",
            call_llm_fn=lambda **_kw: None, main_runtime=None, deliver_extra=dict(_EXTRA),
            chat_user="staff@example.test", language="fr", nora_spoke=True,
        )

    return SimpleNamespace(route=route, posted=posted, classified=classified, reminders=reminders)


def test_the_moment_is_asked_in_code_then_one_reminder_is_set(harness):
    first = harness.route("Rappelle-moi d'appeler le fournisseur de carrelage.")
    assert first["routed"] is True and first["ack"] == R._REMINDER_WHEN["fr"], first
    assert harness.classified == [] and harness.reminders == []

    second = harness.route("Demain à 10h.")
    assert second["ack"] == "C'est noté ✅", second
    assert harness.reminders == ["Rappelle-moi d'appeler le fournisseur de carrelage Demain à 10h."]
    assert harness.classified == [] and R._PENDING_REMINDER == {}


def test_a_bare_yes_leaves_the_question_open(harness):
    harness.route("Rappelle-moi d'appeler le fournisseur de carrelage.")
    harness.route("Oui")
    assert "conv-remind" in R._PENDING_REMINDER
    harness.route("vendredi à 8h")
    assert harness.reminders == ["Rappelle-moi d'appeler le fournisseur de carrelage vendredi à 8h"]


def test_a_refusal_drops_the_question_and_routes_as_usual(harness):
    harness.route("Rappelle-moi d'appeler le fournisseur de carrelage.")
    harness.route("Non, laisse tomber")
    assert harness.reminders == [] and R._PENDING_REMINDER == {}


@pytest.mark.parametrize("message, expected", [
    ("Rappelle-moi d'appeler le fournisseur de carrelage.", True),
    ("Fais-moi penser à envoyer le devis", True),
    ("Remind me to call the tile supplier", True),
    ("Erinnere mich daran, den Lieferanten anzurufen", True),
    ("Ricordami di chiamare il fornitore", True),
    ("Rappelle-moi le nom du client Dupont", False),                    # information, not a reminder
    ("Rappelle-moi demain à 10h d'appeler le fournisseur", False),      # it has its moment
    ("Rappelle-moi tous les lundis de faire la TVA", False),             # a habit: the recurring route
    ("Rappelle-moi de faire un rappel de paiement", False),             # a payment reminder
])
def test_what_is_a_reminder_without_its_moment(message, expected):
    assert R._needs_reminder_moment(message) is expected
