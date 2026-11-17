# //// Neoffice — added file (no upstream equivalent): the cap on free transient requeues of a bounded card (#2810)
"""A card that sets max_retries is not requeued for free for ever (neoffice-maintenance#2810).

A transient provider exit (KANBAN_RATE_LIMIT_EXIT_CODE) is requeued without counting a failure, and upstream retries
it for ever, spaced by the cooldown. For a card whose creator asked for a bound (max_retries), the free requeues are
a streak capped at six; beyond it each transient exit counts, still spaced by the cooldown, and the card stops at its
max_retries. A card without max_retries keeps upstream's behaviour, any other run ends the streak, and a card that is
unblocked, promoted or moved starts a new one.
"""

import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

TRANSIENT = kb.KANBAN_RATE_LIMIT_EXIT_CODE


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB; every worker pid is dead, no crash grace, the default cap."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.delenv("HERMES_KANBAN_TRANSIENT_REQUEUE_LIMIT", raising=False)
    kb.init_db()
    return home


@pytest.fixture
def clock(monkeypatch):
    """Move the kanban's clock forward (the real clock plus a skew, so no wait loop can stall)."""
    real = time.time
    skew = [0.0]
    monkeypatch.setattr(time, "time", lambda: real() + skew[0])

    def advance(seconds):
        skew[0] += seconds

    return advance


def _worker_exits(conn, tid, n, code):
    """One worker of the card that exits with ``code``: claim a run, point it at a dead pid, sweep the crashes."""
    host = kb._claimer_id().split(":", 1)[0]
    pid = 71000 + n
    kb.claim_task(conn, tid, claimer=f"{host}:w{n}")
    conn.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (pid, tid))
    conn.commit()
    kbd._record_worker_exit(pid, code << 8)
    kbd.detect_crashed_workers(conn)
    return kb.get_task(conn, tid)


def test_a_bounded_card_is_requeued_free_six_times_then_counted_and_stopped(
    kanban_home,
):
    with kbc.connect() as conn:
        tid = kb.create_task(
            conn, title="a scan's imputation", assignee="compta", max_retries=2
        )
        for n in range(6):
            task = _worker_exits(conn, tid, n, TRANSIENT)
            assert (task.status, task.consecutive_failures) == ("ready", 0), (
                f"free requeue {n + 1}"
            )
        task = _worker_exits(conn, tid, 6, TRANSIENT)
        assert (task.status, task.consecutive_failures) == ("ready", 1)
        assert "transient provider error 7 times in a row" in task.last_failure_error
        task = _worker_exits(conn, tid, 7, TRANSIENT)
        assert (task.status, task.consecutive_failures) == ("blocked", 2)
        outcomes = [
            r["outcome"]
            for r in conn.execute(
                "SELECT outcome FROM task_runs WHERE task_id=? ORDER BY id", (tid,)
            )
        ]
        assert outcomes == ["rate_limited"] * 6 + ["crashed", "crashed"]


def test_a_card_without_max_retries_keeps_upstreams_free_requeues(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="no bound asked", assignee="compta")
        for n in range(10):
            task = _worker_exits(conn, tid, n, TRANSIENT)
            assert (task.status, task.consecutive_failures) == ("ready", 0), (
                f"requeue {n + 1}"
            )


def test_a_real_crash_ends_the_streak_of_free_requeues(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(
            conn, title="a scan's imputation", assignee="compta", max_retries=2
        )
        for n in range(5):
            _worker_exits(conn, tid, n, TRANSIENT)
        task = _worker_exits(
            conn, tid, 5, 1
        )  # a real crash: counted, and the streak starts again
        assert (task.status, task.consecutive_failures) == ("ready", 1)
        for n in range(6, 12):
            task = _worker_exits(conn, tid, n, TRANSIENT)
            assert (task.status, task.consecutive_failures) == ("ready", 1), (
                f"free requeue {n - 5} after the crash"
            )
        task = _worker_exits(conn, tid, 12, TRANSIENT)
        assert (task.status, task.consecutive_failures) == ("blocked", 2)


def test_an_unblocked_card_starts_a_new_streak(kanban_home, clock, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", raising=False)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="a scan's imputation", assignee="compta", max_retries=2)
        for n in range(8):
            clock(10)
            task = _worker_exits(conn, tid, n, TRANSIENT)
        assert (task.status, task.consecutive_failures) == ("blocked", 2)
        clock(3600)  # the next morning
        assert kb.unblock_task(conn, tid)
        for n in range(8, 10):  # the box is still busy: two more transient exits
            clock(10)
            task = _worker_exits(conn, tid, n, TRANSIENT)
            assert (task.status, task.consecutive_failures) == ("ready", 0), f"exit {n - 7} after the unblock"
        for n in range(10, 14):
            clock(10)
            task = _worker_exits(conn, tid, n, TRANSIENT)
        assert (task.status, task.consecutive_failures) == ("ready", 0)  # six free since the unblock
        clock(10)
        task = _worker_exits(conn, tid, 14, TRANSIENT)
        assert (task.status, task.consecutive_failures) == ("ready", 1)
        clock(10)
        task = _worker_exits(conn, tid, 15, TRANSIENT)
        assert (task.status, task.consecutive_failures) == ("blocked", 2)


def _run(conn, tid, outcome, *, ended_at, metadata=None):
    conn.execute(
        "INSERT INTO task_runs (task_id, status, started_at, ended_at, outcome, metadata) VALUES (?, ?, ?, ?, ?, ?)",
        (tid, "done" if outcome == "completed" else "crashed", ended_at - 5, ended_at, outcome,
         json.dumps(metadata) if metadata else None),
    )


def test_a_success_or_any_other_run_ends_the_streak(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="a scan's imputation", assignee="compta", max_retries=2)
        t = int(time.time())
        for i in range(3):
            _run(conn, tid, "rate_limited", ended_at=t + i)
        _run(conn, tid, "completed", ended_at=t + 10)
        _run(conn, tid, "rate_limited", ended_at=t + 20)
        _run(conn, tid, "crashed", ended_at=t + 21, metadata={"transient_requeues": 7})  # counted past the cap
        conn.commit()
        assert kbd._transient_requeue_streak(conn, tid) == 2
        _run(conn, tid, "blocked", ended_at=t + 30)
        conn.commit()
        assert kbd._transient_requeue_streak(conn, tid) == 0


def test_a_cap_of_zero_keeps_upstreams_free_requeues(kanban_home, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TRANSIENT_REQUEUE_LIMIT", "0")
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="a scan's imputation", assignee="compta", max_retries=2)
        for n in range(10):
            task = _worker_exits(conn, tid, n, TRANSIENT)
            assert (task.status, task.consecutive_failures) == ("ready", 0), f"requeue {n + 1}"


def test_the_counted_transient_exits_are_spaced_like_the_free_ones(kanban_home, clock, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", raising=False)
    cooldown = kb._resolve_rate_limit_cooldown_seconds()
    assert cooldown > 0
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="a scan's imputation", assignee="compta", max_retries=2)
        for n in range(6):
            clock(cooldown + 1)
            _worker_exits(conn, tid, n, TRANSIENT)
            assert kbd.check_respawn_guard(conn, tid) == "rate_limit_cooldown", f"free requeue {n + 1}"
        clock(cooldown + 1)
        task = _worker_exits(conn, tid, 6, TRANSIENT)
        assert (task.status, task.consecutive_failures) == ("ready", 1)
        assert kbd.check_respawn_guard(conn, tid) == "rate_limit_cooldown"  # the 7th waits, as the 6 before
        clock(cooldown + 1)
        assert kbd.check_respawn_guard(conn, tid) is None
        other = kb.create_task(conn, title="another card", assignee="compta", max_retries=2)
        _worker_exits(conn, other, 20, 1)  # a real crash keeps upstream's pace: no cooldown
        assert kbd.check_respawn_guard(conn, other) is None


def test_the_real_dispatcher_tick_stops_a_bounded_card_and_writes_no_fresh_start_event(
    kanban_home, clock, monkeypatch, all_assignees_spawnable
):
    """End to end through dispatch_once: spawn, a transient exit, the tick that reclaims it, the cooldown, again."""
    monkeypatch.delenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", raising=False)
    cooldown = kb._resolve_rate_limit_cooldown_seconds()
    pids = []

    def spawn(task, workspace, board=None):
        pids.append(73000 + len(pids))
        return pids[-1]

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="a scan's imputation", assignee="compta", max_retries=2)
        for n in range(8):
            kbd.dispatch_once(conn, spawn_fn=spawn)
            assert (len(pids), kb.get_task(conn, tid).status) == (n + 1, "running"), f"run {n + 1} spawned"
            kbd._record_worker_exit(pids[-1], TRANSIENT << 8)
            kbd.dispatch_once(conn, spawn_fn=spawn)  # reclaims the exit; the cooldown holds the respawn
            if n < 7:
                assert (len(pids), kb.get_task(conn, tid).status) == (n + 1, "ready"), f"run {n + 1} waits"
            clock(cooldown + 1)
        task = kb.get_task(conn, tid)
        assert (task.status, task.consecutive_failures, len(pids)) == ("blocked", 2, 8)
        kinds = {r[0] for r in conn.execute("SELECT kind FROM task_events WHERE task_id = ?", (tid,))}
        assert not kinds & {"unblocked", "promoted", "status"}, sorted(kinds)
