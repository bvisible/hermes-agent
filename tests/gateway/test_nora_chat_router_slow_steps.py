# //// Neoffice — added file (no upstream equivalent): a slow routing names its slow step (09.10).
"""On the development instance a task was created at once and its ack posted 12.6 s later, with nothing logged in
between (the ack itself took 44 ms at nginx). A routing that takes more than 2 s now says how long each step took:
task creation, subscription, closing the board connection, posting the ack."""
import json
import logging
import sys
import types
from types import SimpleNamespace

import pytest

from gateway import nora_chat_router as R

_CB = "https://erp.example.test/api/method/nora.api.v2.hermes_callback.deliver"


@pytest.fixture(autouse=True)
def fresh_state():
    for d in (R._LAST_ROUTE, R._PENDING_SPACE, R._PENDING_NOTE, R._CONV_HISTORY, R._SPACE_ASKED):
        d.clear()
    yield
    for d in (R._LAST_ROUTE, R._PENDING_SPACE, R._PENDING_NOTE, R._CONV_HISTORY, R._SPACE_ASKED):
        d.clear()


class _Clock:
    """monotonic() that moves forward by the next step each time it is read."""

    def __init__(self, steps):
        self.now, self.steps = 100.0, list(steps)

    def monotonic(self):
        self.now += self.steps.pop(0) if self.steps else 0.0
        return self.now

    def time(self):
        return 1_700_000_000.0


class _Resp:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return json.dumps({"message": "ok"}).encode()


def _route(monkeypatch, clock):
    kanban_db = types.SimpleNamespace(create_task=lambda conn, **kw: "t_slow")
    connect = types.SimpleNamespace(connect=lambda board=None: SimpleNamespace(close=lambda: None))
    notify = types.SimpleNamespace(add_notify_sub=lambda *a, **kw: None)
    pkg = sys.modules.get("hermes_cli") or types.ModuleType("hermes_cli")
    monkeypatch.setitem(sys.modules, "hermes_cli", pkg)
    for name, mod in (("kanban_db", kanban_db), ("kanban_db_connect", connect), ("kanban_db_notify", notify)):
        monkeypatch.setattr(pkg, name, mod, raising=False)
        monkeypatch.setitem(sys.modules, f"hermes_cli.{name}", mod)
    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _Resp())
    monkeypatch.setattr(R, "_asks_fast_answer", lambda *a, **k: False)
    monkeypatch.setattr(R, "classify", lambda message, **kw: "ventes")
    monkeypatch.setattr(R, "_time_thread", clock)
    return R.route_chat_message(
        message="Combien de devis ouverts pour ce client ?", session_chat_id="webhook:nora_chat:slow",
        conversation_id="conv-slow", thread_id=None, user_id="u", notifier_profile="default",
        idempotency_key="k-slow", call_llm_fn=lambda **_kw: None, main_runtime=None,
        deliver_extra={"callback_url": _CB, "callback_token": "tok", "conversation_id": "conv-slow"},
        chat_user="staff@example.test", language="fr", nora_spoke=True,
    )


def test_a_slow_routing_names_its_slow_step(monkeypatch, caplog):
    # start, created (+0.1), subscribed (+3.0), closed (+0.1), acked (+0.1)
    clock = _Clock([0.0, 0.1, 3.0, 0.1, 0.1])
    with caplog.at_level(logging.WARNING, logger=R.logger.name):
        decision = _route(monkeypatch, clock)
    assert decision["routed"] is True and decision["task_id"] == "t_slow"
    slow = [r.getMessage() for r in caplog.records if "slow routing" in r.getMessage()]
    assert slow == ["nora_chat_router: slow routing for task t_slow: create 0.1 s, subscription 3.0 s, "
                    "close 0.1 s, ack 0.1 s"]


def test_a_quick_routing_says_nothing(monkeypatch, caplog):
    clock = _Clock([0.0, 0.1, 0.1, 0.1, 0.1])
    with caplog.at_level(logging.WARNING, logger=R.logger.name):
        _route(monkeypatch, clock)
    assert not [r for r in caplog.records if "slow routing" in r.getMessage()]

