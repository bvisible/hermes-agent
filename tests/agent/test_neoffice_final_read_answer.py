# //// Neoffice — added file (no upstream equivalent): a read that declares its final answer closes the
# //// worker's card without a model turn, only for the tools the profile names and a task the router marked (#3081).
"""A pole's one-call read answers by itself: the card closes on the tool's own sentence.

The worker's second model turn only rewrote that sentence into kanban_complete, 2.5 to 4 s per question.
Absent key, nothing changes; a task the router did not mark as one read (a request that asks two things) keeps the
usual turn; any doubt (a second call, a cut result, a refused completion) leaves the usual turn.
"""

from __future__ import annotations

import json
import pathlib
from types import SimpleNamespace

import pytest

from agent.neoffice_kanban_breaker import (
    neoffice_final_read_answer,
    neoffice_final_read_tools,
    neoffice_kanban_no_progress_breaker,
)

ANSWER = "Votre article le plus vendu à Carla Exemplar en 2026 est la chaise Fictiva : 42 pièces, 3 150 CHF."
TOOL = "mcp__neoffice_ventes__frappe_top_items"


@pytest.fixture
def card(monkeypatch, tmp_path):
    """An isolated board holding one claimed ventes card; the process is its worker."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "ventes")
    for name in ("HERMES_KANBAN_BOARD", "HERMES_KANBAN_GOAL_MODE", "NEOFFICE_KANBAN_HESITATION_ROUNDS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="Quel produit je lui ai le plus vendu ?", assignee="ventes")
        kb.claim_task(conn, tid)
        run_id = kb.latest_run(conn, tid).id
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))  # the dispatcher pins it on a real worker
    return SimpleNamespace(id=tid, home=home)


def _mark(card, shape="one_read", by_a_run=False):
    """The router's mark, as it posts it when it creates the task (no run); by_a_run posts it from the run instead."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    conn = kbc.connect()
    try:
        run_id = kb.latest_run(conn, card.id).id if by_a_run else None
        kb._append_event(conn, card.id, "neoffice_answer_shape", {"shape": shape, "set_by": "router"}, run_id=run_id)
        conn.commit()
    finally:
        conn.close()


def _key(card, tools):
    (card.home / "config.yaml").write_text(
        "kanban:\n  neoffice_final_read_tools:\n" + "".join(f"    - {t}\n" for t in tools))


def _status(tid):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    conn = kbc.connect()
    try:
        return kb.get_task(conn, tid).status, getattr(kb.latest_run(conn, tid), "summary", None)
    finally:
        conn.close()


def _call(name=TOOL, call_id="call_1"):
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments='{"customer": "C-1"}'))


def _result(read, call_id="call_1"):
    text = read if isinstance(read, str) else json.dumps(read, ensure_ascii=False)
    return {"role": "tool", "tool_call_id": call_id, "content": json.dumps({"result": text}, ensure_ascii=False)}


def _read(**extra):
    return {"success": True, "items": [{"item": "Chaise Fictiva", "qty": 42}], "final_answer": ANSWER, **extra}


def _round(agent, calls, messages):
    return neoffice_kanban_no_progress_breaker(
        agent, assistant_message=SimpleNamespace(tool_calls=calls), messages=messages,
        final_response=None, _turn_exit_reason=None,
    )


def test_the_key_absent_changes_nothing(card):
    messages = [_result(_read())]
    verdict = _round(SimpleNamespace(session_id=None), [_call()], messages)
    assert verdict.action == "fallthrough" and verdict.final_response is None
    assert _status(card.id)[0] == "running" and len(messages) == 1


def test_a_named_read_with_its_answer_closes_the_card(card):
    _key(card, [TOOL])
    _mark(card)
    messages = [_result(_read())]
    verdict = _round(SimpleNamespace(session_id=None), [_call()], messages)
    assert verdict.action == "break"
    assert verdict.final_response == ANSWER and verdict._turn_exit_reason == "neoffice_final_read"
    assert _status(card.id) == ("done", ANSWER)
    assert messages[-1] == {"role": "assistant", "content": ANSWER}


@pytest.mark.parametrize("calls, messages", [
    pytest.param([_call(), _call(call_id="call_2")], [_result(_read()), _result(_read(), "call_2")], id="two-calls"),
    pytest.param([_call("mcp__neoffice_ventes__frappe_gross_profit")], [_result(_read())], id="tool-not-named"),
    pytest.param([_call("mcp__neoffice_ventes__frappe_top_items_v2")], [_result(_read())], id="longer-name"),
    pytest.param([_call("mcp__a_third_party__frappe_top_items")], [_result(_read())], id="same-tool-other-server"),
    pytest.param([_call()], [_result({"_truncated": True, "_full_length": 9000, "preview": "…"})], id="cut-result"),
    pytest.param([_call()], [_result(_read(success=False))], id="not-a-success"),
    pytest.param([_call()], [_result(_read(error="no such customer"))], id="an-error"),
    pytest.param([_call()], [_result({"success": True, "items": []})], id="no-field"),
    pytest.param([_call()], [_result(_read(final_answer="   "))], id="empty-answer"),
    pytest.param([_call()], [_result(_read(final_answer="x" * 1501))], id="too-long"),
    pytest.param([_call()], [_result(_read(final_answer=42))], id="not-text"),
    pytest.param([_call()], [_result("not json")], id="not-json"),
    pytest.param([_call()], [{"role": "tool", "tool_call_id": "call_1", "content": json.dumps({"error": "boom"})}],
                 id="tool-error"),
    pytest.param([_call()], [_result(_read(), call_id="call_9")], id="other-call-id"),
])
def test_any_doubt_leaves_the_usual_turn(card, calls, messages):
    _key(card, [TOOL])
    _mark(card)
    before = list(messages)
    verdict = _round(SimpleNamespace(session_id=None), calls, messages)
    assert verdict.action == "fallthrough" and verdict.final_response is None
    assert _status(card.id)[0] == "running" and messages == before


@pytest.mark.parametrize("mark", [
    pytest.param(None, id="not-marked: a request that asks two things"),
    pytest.param({"shape": "two_reads"}, id="marked-otherwise"),
    pytest.param({"shape": "one_read", "by_a_run": True}, id="marked-by-a-run-not-the-router"),
])
def test_without_the_routers_mark_the_card_keeps_its_model_turn(card, mark):
    _key(card, [TOOL])
    if mark:
        _mark(card, mark["shape"], by_a_run=mark.get("by_a_run", False))
    messages = [_result(_read())]
    verdict = _round(SimpleNamespace(session_id=None), [_call()], messages)
    assert verdict.action == "fallthrough" and verdict.final_response is None
    assert _status(card.id)[0] == "running" and len(messages) == 1


def test_a_bare_tool_name_in_the_key_names_no_mcp_tool(card):
    _key(card, ["frappe_top_items"])
    _mark(card)
    assert _round(SimpleNamespace(session_id=None), [_call()], [_result(_read())]).action == "fallthrough"
    assert _status(card.id)[0] == "running"


def test_a_bare_name_in_the_key_never_closes_on_a_builtin_tool_of_that_name(card):
    _key(card, ["web_extract"])
    _mark(card)
    assert _round(SimpleNamespace(session_id=None), [_call("web_extract")], [_result(_read())]).action == "fallthrough"
    assert _status(card.id)[0] == "running"


def _earlier_run(card, outcome):
    """A run of the task that ended before the current one, as the dispatcher records it."""
    from hermes_cli import kanban_db_connect as kbc

    conn = kbc.connect()
    try:
        conn.execute("INSERT INTO task_runs (task_id, status, started_at, ended_at, outcome) VALUES (?, ?, 1, 2, ?)",
                     (card.id, "done" if outcome == "completed" else outcome, outcome))
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize("outcome, closes", [("completed", False), ("blocked", False), ("crashed", True),
                                             ("timed_out", True)])
def test_a_reopened_or_unblocked_task_no_longer_reads_once(card, outcome, closes):
    _key(card, [TOOL])
    _mark(card)
    _earlier_run(card, outcome)
    verdict = _round(SimpleNamespace(session_id=None), [_call()], [_result(_read())])
    assert (verdict.action != "fallthrough") is closes, outcome
    assert (_status(card.id)[0] == "done") is closes


def test_only_the_tasks_first_business_round(card):
    _key(card, [TOOL])
    _mark(card)
    agent = SimpleNamespace(session_id=None)
    first = [_result(_read(final_answer=None), "call_0")]
    assert _round(agent, [_call(call_id="call_0")], first).action == "fallthrough"
    messages = first + [_result(_read())]
    assert _round(agent, [_call()], messages).action == "fallthrough"
    assert _status(card.id)[0] == "running"


def test_a_goal_mode_card_keeps_its_continuation(card, monkeypatch):
    _key(card, [TOOL])
    _mark(card)
    monkeypatch.setenv("HERMES_KANBAN_GOAL_MODE", "1")
    assert _round(SimpleNamespace(session_id=None), [_call()], [_result(_read())]).action == "fallthrough"
    assert _status(card.id)[0] == "running"


def test_a_refused_completion_leaves_the_usual_turn(card, monkeypatch):
    from tools import kanban_tools

    _key(card, [TOOL])
    _mark(card)
    monkeypatch.setattr(kanban_tools, "_handle_complete",
                        lambda args, **kw: json.dumps({"error": "kanban_complete blocked: a guard said no"}))
    messages = [_result(_read())]
    verdict = _round(SimpleNamespace(session_id=None), [_call()], messages)
    assert verdict.action == "fallthrough" and len(messages) == 1
    assert _status(card.id)[0] == "running"


def test_the_completion_goes_through_kanban_completes_handler_with_the_session(card, monkeypatch):
    from tools import kanban_tools

    _key(card, [TOOL])
    _mark(card)
    seen = []
    real = kanban_tools._handle_complete

    def spy(args, **kw):
        seen.append((args, kw))
        return real(args, **kw)

    monkeypatch.setattr(kanban_tools, "_handle_complete", spy)
    _round(SimpleNamespace(session_id="local-abc"), [_call()], [_result(_read())])
    assert seen == [({"summary": ANSWER}, {"session_id": "local-abc"})]


def test_the_orchestrator_is_untouched(card, monkeypatch):
    _key(card, [TOOL])
    _mark(card)
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    assert _round(SimpleNamespace(session_id=None), [_call()], [_result(_read())]).action == "fallthrough"


def test_only_the_exact_full_name_counts():
    read = [_result(_read())]
    for name, expected in ((TOOL, ANSWER), ("frappe_top_items", ""), ("mcp__a_third_party__frappe_top_items", ""),
                           (TOOL + "_v2", ""), (TOOL + "__x", "")):
        message = SimpleNamespace(tool_calls=[_call(name)])
        assert neoffice_final_read_answer(message, read, frozenset({TOOL})) == expected, name


@pytest.mark.parametrize("config, expected", [
    ({}, frozenset()),
    ({"kanban": {}}, frozenset()),
    ({"kanban": {"neoffice_final_read_tools": None}}, frozenset()),
    ({"kanban": {"neoffice_final_read_tools": TOOL}}, frozenset()),
    ({"kanban": {"neoffice_final_read_tools": [f" {TOOL} ", "", 3]}}, frozenset({TOOL})),
    ({"kanban": {"neoffice_final_read_tools": ["web_extract", "frappe_top_items", TOOL]}}, frozenset({TOOL})),
    ({"kanban": {"neoffice_final_read_tools": {"frappe_top_items": True}}}, frozenset()),
])
def test_the_key_reads_a_list_of_names(config, expected):
    assert neoffice_final_read_tools(config) == expected
