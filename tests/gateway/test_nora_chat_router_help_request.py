# //// Neoffice — added file (no upstream equivalent): a request for help is never left to DIRECT (#1174).
"""« Comment faire une note de crédit ? » goes to the support pole, not to the orchestrator alone.

nora reads a request for help in code (help_intent.read_help_request) and says so in the message
(`help_request: "ask"`). The classifier's capability rule sent such questions to DIRECT, as answered in a
sentence; the orchestrator then searched its doctrine wiki, which does not hold the user manual, for three
minutes and answered that it could not finish. The support pole answered the same question in 19 s.
"""
import io
import json
import sys
import types
from types import SimpleNamespace

import pytest

from gateway import nora_chat_router as R

_CB = "https://erp.example.test/api/method/nora.api.v2.hermes_callback.deliver"
_EXTRA = {"callback_url": _CB, "callback_token": "tok", "conversation_id": "conv-help"}


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


def _fake_kanban(monkeypatch):
    created = []
    kanban_db = types.SimpleNamespace(create_task=lambda conn, **kw: created.append(kw) or "t_help")
    connect = types.SimpleNamespace(connect=lambda board=None: SimpleNamespace(close=lambda: None))
    notify = types.SimpleNamespace(add_notify_sub=lambda *a, **kw: None)
    pkg = sys.modules.get("hermes_cli") or types.ModuleType("hermes_cli")
    monkeypatch.setitem(sys.modules, "hermes_cli", pkg)
    for name, mod in (("kanban_db", kanban_db), ("kanban_db_connect", connect), ("kanban_db_notify", notify)):
        monkeypatch.setattr(pkg, name, mod, raising=False)
        monkeypatch.setitem(sys.modules, f"hermes_cli.{name}", mod)
    return created


def _route(monkeypatch, message, *, verdict, help_request="ask", page_context=None):
    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", _Http())
    monkeypatch.setattr(R, "_asks_fast_answer", lambda *a, **k: False)
    monkeypatch.setattr(R, "classify", lambda message, **kw: verdict)
    return R.route_chat_message(
        message=message, session_chat_id="webhook:nora_chat:test", conversation_id="conv-help",
        thread_id=None, user_id="u", notifier_profile="default", idempotency_key="k",
        call_llm_fn=lambda **_kw: None, main_runtime=None, deliver_extra=dict(_EXTRA),
        chat_user="staff@example.test", language="fr", page_context=page_context, nora_spoke=True,
        help_request=help_request)


@pytest.mark.parametrize("message", [
    "comment faire une note de crédit ?",
    "Comment on fait un avoir ?",
    "aide-moi à saisir une facture fournisseur",
])
def test_a_request_for_help_classified_direct_goes_to_support(monkeypatch, message):
    created = _fake_kanban(monkeypatch)
    decision = _route(monkeypatch, message, verdict="DIRECT")
    assert decision["routed"] is True and decision["category"] == "support"
    assert created[-1]["assignee"] == "support" and message in created[-1]["body"]


def test_the_same_question_without_the_flag_is_left_as_it_was(monkeypatch):
    created = _fake_kanban(monkeypatch)
    decision = _route(monkeypatch, "comment faire une note de crédit ?", verdict="DIRECT", help_request=None)
    assert decision["routed"] is False and decision["category"] == "DIRECT" and created == []


@pytest.mark.parametrize("flag", ["topic", "yes", "", 1, True])
def test_only_an_ask_counts(monkeypatch, flag):
    created = _fake_kanban(monkeypatch)
    decision = _route(monkeypatch, "note de crédit", verdict="DIRECT", help_request=flag)
    assert decision["routed"] is False and created == []


def test_a_pole_the_classifier_chose_is_kept(monkeypatch):
    created = _fake_kanban(monkeypatch)
    decision = _route(monkeypatch, "comment faire un devis", verdict="ventes")
    assert decision["routed"] is True and created[-1]["assignee"] == "ventes"


def test_small_talk_settled_in_code_stays_small_talk(monkeypatch):
    created = _fake_kanban(monkeypatch)
    decision = _route(monkeypatch, "Bonjour", verdict="DIRECT")
    assert created == [] and decision["category"] == "DIRECT"


def test_the_webhook_passes_only_an_ask():
    """The webhook hands the flag over only when it is the one value nora sends."""
    from pathlib import Path

    src = Path(__file__).resolve().parents[2].joinpath("gateway", "platforms", "webhook.py").read_text(encoding="utf-8")
    assert 'help_request=("ask" if payload.get("help_request") == "ask" else None)' in src
