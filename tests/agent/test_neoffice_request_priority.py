# //// Neoffice — added file (no upstream equivalent): a worker somebody waits on is served first.
"""The Olares proxy gives priority 10 to any prompt over 16 KB unless the request says otherwise, so a
chat worker's turn queued behind skill reviews and the nightly memory pass (2026-09-25: a first agent
call slow in 45-74 % of cases when requests wait). A chat-facing kanban worker sends priority 0."""
from types import SimpleNamespace

import pytest

from agent import chat_completion_helpers as cch
from agent import neoffice_kanban_breaker as breaker

OLARES = "https://olares1.noraai.ch/v1"


def _agent(**kw):
    return SimpleNamespace(api_mode="chat_completions", base_url=OLARES, **kw)


@pytest.fixture
def waited_on(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_chat")
    calls = []
    monkeypatch.setattr(breaker, "_neoffice_task_has_subscriber", lambda tid: calls.append(tid) or True)
    return calls


def test_a_worker_somebody_waits_on_asks_for_priority_0(waited_on):
    kwargs = breaker.neoffice_request_priority(_agent(), {"model": "nora", "extra_body": {"reasoning": {"enabled": False}}})
    assert kwargs["extra_body"] == {"reasoning": {"enabled": False}, "priority": 0}


def test_an_explicit_priority_is_kept(waited_on):
    kwargs = breaker.neoffice_request_priority(_agent(), {"extra_body": {"priority": 5}})
    assert kwargs["extra_body"]["priority"] == 5


@pytest.mark.parametrize("agent", (
    _agent(_turn_origin="background_review"),               # the post-task review can wait
    SimpleNamespace(api_mode="chat_completions", base_url="https://api.openai.com/v1"),  # would reject it
    SimpleNamespace(api_mode="anthropic_messages", base_url=OLARES),
))
def test_no_priority_where_it_does_not_belong(waited_on, agent):
    assert "extra_body" not in breaker.neoffice_request_priority(agent, {"model": "nora"})


def test_no_priority_outside_a_worker_or_without_a_subscriber(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    assert "extra_body" not in breaker.neoffice_request_priority(_agent(), {"model": "nora"})
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_cron")
    monkeypatch.setattr(breaker, "_neoffice_task_has_subscriber", lambda tid: False)
    assert "extra_body" not in breaker.neoffice_request_priority(_agent(), {"model": "nora"})


def test_the_subscriber_is_read_once_per_task(monkeypatch):
    monkeypatch.setattr(breaker, "_NEOFFICE_SUBSCRIBED", {})
    reads = []

    class _Conn:
        def close(self):
            pass

    import sys
    import types
    connect = types.SimpleNamespace(connect=lambda board=None: _Conn())
    notify = types.SimpleNamespace(list_notify_subs=lambda conn, tid: reads.append(tid) or [{"platform": "webhook"}])
    pkg = sys.modules.get("hermes_cli") or types.ModuleType("hermes_cli")
    monkeypatch.setitem(sys.modules, "hermes_cli", pkg)
    for name, mod in (("kanban_db_connect", connect), ("kanban_db_notify", notify)):
        monkeypatch.setattr(pkg, name, mod, raising=False)
        monkeypatch.setitem(sys.modules, f"hermes_cli.{name}", mod)
    assert breaker._neoffice_task_has_subscriber("t_x") and breaker._neoffice_task_has_subscriber("t_x")
    assert reads == ["t_x"]


def test_every_request_of_the_turn_goes_through_it(waited_on, monkeypatch):
    monkeypatch.setattr(cch, "_build_api_kwargs_for_mode", lambda agent, msgs, tools=None: {"model": "nora"})
    import agent.opencode_affinity as affinity
    monkeypatch.setattr(affinity, "merge_session_affinity_headers", lambda kwargs, *a: kwargs)
    kwargs = cch.build_api_kwargs(_agent(provider="custom", session_id="s"), [{"role": "user", "content": "q"}])
    assert kwargs["extra_body"]["priority"] == 0
