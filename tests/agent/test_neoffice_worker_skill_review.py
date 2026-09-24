# //// Neoffice — added file (no upstream equivalent): a pole worker waits, bounded, for the
# //// skill review of a task that ended well, and does not review a run that did not.
"""A kanban worker is a one-shot process: without a wait, its daemon review thread dies with it.

Measured on the dev instance (2026-09-24): 251 reviews started by pole workers since May,
not one reached skill_manage — the worker closed about 3 s after the review's first model
call. finalize_turn now holds a worker whose card is done until the review ends, never past
its wait budget, and skips the review of a halted, blocked or failed run.
"""

from __future__ import annotations

import pathlib
import threading
import time
from unittest.mock import MagicMock

import pytest

from agent import turn_finalizer
from agent.turn_finalizer import finalize_turn
from run_agent import AIAgent


@pytest.fixture(autouse=True)
def review_slot(monkeypatch, tmp_path):
    """Each test starts without the machine-wide review slot, its lock file in the test's own dir."""
    import tempfile

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    held = turn_finalizer._NEOFFICE_REVIEW_SLOT
    if held is not None:
        held.close()
    monkeypatch.setattr(turn_finalizer, "_NEOFFICE_REVIEW_SLOT", None)
    yield tmp_path
    if turn_finalizer._NEOFFICE_REVIEW_SLOT is not None:
        turn_finalizer._NEOFFICE_REVIEW_SLOT.close()


@pytest.fixture
def worker_card(monkeypatch, tmp_path):
    """An isolated board holding one claimed card; the process is that card's worker."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "compta")
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_NEOFFICE_REVIEW_WAIT_SECONDS", raising=False)
    monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="impute an invoice", assignee="compta")
        kb.claim_task(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    return tid


def _finish_card(tid: str, how: str) -> None:
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    conn = kbc.connect()
    try:
        if how == "done":
            assert kb.complete_task(conn, tid, result="6400", summary="6400")
        else:
            assert kb.block_task(conn, tid, reason="no chart of accounts")
    finally:
        conn.close()


def _agent_with_review(seconds: float, finished: threading.Event, stop: threading.Event | None = None):
    agent = AIAgent(
        model="openai/gpt-4o-mini", provider="openrouter", api_key="sk-dummy",
        base_url="https://openrouter.ai/api/v1", quiet_mode=True, skip_context_files=True,
        skip_memory=True, platform="cli",
    )

    def spawn(**_kwargs):
        def review():
            (stop or threading.Event()).wait(seconds)
            finished.set()

        # The name run_agent._spawn_background_review_now gives the real review thread.
        threading.Thread(target=review, daemon=True, name="bg-review").start()

    agent._spawn_background_review = MagicMock(side_effect=spawn)
    for name in ("_save_trajectory", "_cleanup_task_resources", "_persist_session", "clear_interrupt",
                 "_sync_external_memory_for_turn", "_emit_status", "_safe_print",
                 "_apply_persist_user_message_override"):
        setattr(agent, name, MagicMock())
    agent._session_messages = []
    agent._file_mutation_verifier_enabled = lambda: False
    agent._stream_callback = None
    agent._skill_nudge_interval = 10
    agent._iters_since_skill = 14  # a long task: fourteen tool iterations
    agent.valid_tool_names = {"skill_manage"}
    agent.iteration_budget = MagicMock(remaining=100, used=14, max_total=100)
    agent.max_iterations = 40
    agent.context_compressor = None
    agent._turn_preflight_display_snapshot = None
    agent._turn_received_provider_response = False
    agent.model = "nora"
    agent.session_id = "worker-session"
    agent._turn_failed_file_mutations = {}
    agent._db_flush_scan_prefix = None
    return agent


def _finalize(agent, exit_reason: str) -> float:
    started = time.monotonic()
    finalize_turn(
        agent, final_response="Compte 6400", api_call_count=14, interrupted=False, failed=False,
        messages=[{"role": "assistant", "content": "Compte 6400"}], conversation_history=[],
        effective_task_id="worker", turn_id="worker-turn", user_message="work kanban task",
        original_user_message="work kanban task", _should_review_memory=False,
        _turn_exit_reason=exit_reason,
    )
    return time.monotonic() - started


def test_a_worker_whose_card_is_done_waits_for_its_review(worker_card):
    _finish_card(worker_card, "done")
    finished = threading.Event()
    agent = _agent_with_review(0.4, finished)

    _finalize(agent, "kanban_terminal_tool(status=done)")

    agent._spawn_background_review.assert_called_once()
    assert agent._spawn_background_review.call_args.kwargs["review_skills"] is True
    assert finished.is_set(), "the worker returned while its skill review was still running"


def test_the_wait_never_passes_its_budget(worker_card, monkeypatch):
    monkeypatch.setenv("HERMES_NEOFFICE_REVIEW_WAIT_SECONDS", "0.3")
    _finish_card(worker_card, "done")
    finished, stop = threading.Event(), threading.Event()
    agent = _agent_with_review(30, finished, stop)
    try:
        took = _finalize(agent, "kanban_terminal_tool(status=done)")
        assert took < 5, f"the worker waited {took:.1f}s for a 0.3s budget"
        assert not finished.is_set()
    finally:
        stop.set()


@pytest.mark.parametrize("card, exit_reason, halted", [
    ("done", "kanban_terminal_tool(status=done)", True),        # a guardrail stopped the loop
    ("done", "kanban_no_progress_breaker(streak=3)", False),     # the breaker ended it
    ("blocked", "kanban_terminal_tool(status=blocked)", False),  # the worker gave up
    ("done", "max_iterations_reached(40/40)", False),            # out of budget
])
def test_a_run_that_did_not_end_well_is_not_reviewed(worker_card, card, exit_reason, halted):
    _finish_card(worker_card, card)
    finished = threading.Event()
    agent = _agent_with_review(0.1, finished)
    if halted:
        agent._tool_guardrail_halt_decision = MagicMock()

    _finalize(agent, exit_reason)

    agent._spawn_background_review.assert_not_called()


def test_outside_a_worker_the_review_stays_in_the_background(worker_card, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    finished, stop = threading.Event(), threading.Event()
    agent = _agent_with_review(30, finished, stop)
    try:
        took = _finalize(agent, "text_response(finish_reason=stop)")
        agent._spawn_background_review.assert_called_once()
        assert took < 5 and not finished.is_set(), "an interactive turn must not wait for its review"
    finally:
        stop.set()


def test_one_worker_reviews_at_a_time_on_a_machine(worker_card, review_slot):
    import fcntl
    import os

    _finish_card(worker_card, "done")
    # Another worker of this machine is reviewing: it holds the slot.
    other = open(os.path.join(str(review_slot), f"hermes-neoffice-skill-review-{os.getuid()}.lock"), "a")
    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        agent = _agent_with_review(0.1, threading.Event())
        _finalize(agent, "kanban_terminal_tool(status=done)")
        agent._spawn_background_review.assert_not_called()
    finally:
        other.close()

    # Its review over, the next worker gets the slot.
    agent = _agent_with_review(0.1, threading.Event())
    _finalize(agent, "kanban_terminal_tool(status=done)")
    agent._spawn_background_review.assert_called_once()


def test_the_thread_name_is_the_one_run_agent_gives_the_review():
    # Upstream renaming the thread would make the wait find nothing, in silence.
    source = (pathlib.Path(__file__).resolve().parents[2] / "run_agent.py").read_text(encoding="utf-8")
    assert f'name="{turn_finalizer._NEOFFICE_REVIEW_THREAD_NAME}"' in source
