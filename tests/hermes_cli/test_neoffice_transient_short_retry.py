# //// Neoffice — added file (no upstream equivalent): a transient exit that is no quota is retried in seconds, and
# //// the second one in a row is said to the person (neoffice-maintenance#3118)
"""A transient exit that is no quota wall is retried in seconds, and the second in a row is said (#3118).

Upstream books every transient provider exit (quota, overloaded, server_error, timeout, an empty stream) as the same
rate-limited requeue, spaced by the quota cooldown of 300 s: a card whose model gave one empty stream kept its person
waiting five minutes in silence. The owner now records the turn's failure reason with the exit; the dispatcher keeps
the cooldown for a quota or billing wall, waits seconds for server_error, timeout and overloaded, and at the second such
exit in a row blocks the card on a sentence the person reads. These tests drive the managed path the dispatcher
spawns (``kanban_worker_client``), through the real dead-worker sweep, on a temporary board.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

TRANSIENT = kb.KANBAN_RATE_LIMIT_EXIT_CODE
SENTENCE = ("Je n'ai pas pu vous répondre, le service ne répond pas en ce moment ; pouvez-vous reposer votre "
            "question dans quelques minutes ?")


@pytest.fixture
def conn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    for name in ("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "HERMES_KANBAN_TRANSIENT_RETRY_SECONDS",
                 "HERMES_KANBAN_TRANSIENT_REQUEUE_LIMIT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kbd._recent_worker_exits.clear()
    import agent.i18n

    monkeypatch.setattr(agent.i18n, "get_language", lambda: "fr")
    kb.init_db()
    with kbc.connect() as c:
        yield c


@pytest.fixture
def clock(monkeypatch):
    """Move the kanban's clock forward (the real clock plus a skew)."""
    real = time.time
    skew = [0.0]
    monkeypatch.setattr(time, "time", lambda: real() + skew[0])

    def advance(seconds):
        skew[0] += seconds

    return advance


def _dead_pid() -> int:
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def _managed_exit(conn, tid, reason=None, code=TRANSIENT):
    """One managed run of the card that ends on ``code``, its authority recording ``reason`` as the owner does."""
    host = kb._claimer_id().split(":", 1)[0]
    task = kb.claim_task(conn, tid, claimer=f"{host}:owner")
    assert task is not None
    pid = _dead_pid()
    kbd._set_worker_pid(conn, tid, pid)
    payload = {"pid": pid, "claim_lock": task.claim_lock, "exit_code": code, "last_output": ""}
    if reason:
        payload["transient_reason"] = reason
    with kb.write_txn(conn):
        kb._append_event(conn, tid, "worker_result", payload, run_id=task.current_run_id)
    kbd.detect_crashed_workers(conn)
    return kb.get_task(conn, tid)


def _block_reasons(conn, tid):
    return [json.loads(p).get("reason") for (p,) in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='blocked' ORDER BY id", (tid,))]


def test_a_short_transient_waits_seconds_and_a_quota_the_cooldown(conn, clock):
    short = kb.create_task(conn, title="a question", assignee="compta")
    quota = kb.create_task(conn, title="another question", assignee="compta")
    assert _managed_exit(conn, short, "server_error").status == "ready"
    assert _managed_exit(conn, quota, "rate_limit").status == "ready"
    assert kbd.check_respawn_guard(conn, short) == "rate_limit_cooldown"
    assert kbd.check_respawn_guard(conn, quota) == "rate_limit_cooldown"
    clock(16)
    assert kbd.check_respawn_guard(conn, short) is None, "a server error waits seconds, not the quota cooldown"
    assert kbd.check_respawn_guard(conn, quota) == "rate_limit_cooldown"
    clock(300)
    assert kbd.check_respawn_guard(conn, quota) is None


@pytest.mark.parametrize("reason", ["server_error", "timeout", "overloaded"])
def test_the_second_short_transient_in_a_row_blocks_the_card_on_the_sentence(conn, reason):
    tid = kb.create_task(conn, title="a question", assignee="compta")
    assert _managed_exit(conn, tid, reason).status == "ready"
    assert _block_reasons(conn, tid) == []
    task = _managed_exit(conn, tid, reason)
    assert (task.status, task.block_kind) == ("blocked", "needs_input")
    assert _block_reasons(conn, tid) == [SENTENCE], "the person reads what to do, through the subscribers' notifier"


@pytest.mark.parametrize("reasons", [
    pytest.param(["rate_limit"] * 4, id="quota-wall"),
    pytest.param(["billing"] * 3, id="billing-wall"),
    pytest.param(["server_error", "rate_limit", "server_error"], id="a-quota-between"),
    pytest.param([None, None, None], id="no-reason-recorded"),
])
def test_what_is_no_second_short_exit_in_a_row_is_never_said(conn, reasons):
    tid = kb.create_task(conn, title="a question", assignee="compta")
    for reason in reasons:
        assert _managed_exit(conn, tid, reason).status == "ready", reason
    assert _block_reasons(conn, tid) == []


def test_a_run_recorded_without_a_reason_keeps_upstreams_cooldown(conn, clock):
    tid = kb.create_task(conn, title="a question", assignee="compta")
    _managed_exit(conn, tid, None)
    clock(16)
    assert kbd.check_respawn_guard(conn, tid) == "rate_limit_cooldown"
    clock(300)
    assert kbd.check_respawn_guard(conn, tid) is None


def test_the_persons_answer_starts_again_from_one(conn):
    tid = kb.create_task(conn, title="a question", assignee="compta")
    _managed_exit(conn, tid, "server_error")
    assert _managed_exit(conn, tid, "server_error").status == "blocked"
    assert kb.unblock_task(conn, tid)
    assert _managed_exit(conn, tid, "server_error").status == "ready", "a new streak after the person answered"
    assert _block_reasons(conn, tid) == [SENTENCE]


def test_a_card_in_cooldown_holds_no_other_card(conn):
    waiting = kb.create_task(conn, title="a question", assignee="compta")
    other = kb.create_task(conn, title="someone else's question", assignee="rh")
    _managed_exit(conn, waiting, "rate_limit")
    assert kbd.check_respawn_guard(conn, waiting) == "rate_limit_cooldown"
    assert kbd.check_respawn_guard(conn, other) is None, "the cooldown is the card's, never the board's"


def test_the_owner_records_the_reason_with_a_transient_exit_only(conn, monkeypatch):
    from gateway import session_kanban as sk

    monkeypatch.setattr(sk, "_bound_worker_matches", lambda _conn, _context: True)
    tid = kb.create_task(conn, title="a question", assignee="compta")
    task = kb.claim_task(conn, tid, claimer=f"{kb._claimer_id().split(':', 1)[0]}:owner")
    db = conn.execute("PRAGMA database_list").fetchone()[2]
    context = {"db": db, "task_id": tid, "claim_lock": task.claim_lock, "run_id": task.current_run_id}
    sk._record_worker_result(context, TRANSIENT, "", transient_reason="server_error")
    sk._record_worker_result(context, 1, "", transient_reason="server_error")
    sk._record_worker_result(context, TRANSIENT, "")
    payloads = [json.loads(p) for (p,) in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='worker_result' ORDER BY id", (tid,))]
    assert [p.get("transient_reason") for p in payloads] == ["server_error", None, None]


@pytest.mark.parametrize("lang", ["fr", "de", "it", "en"])
def test_the_question_is_one_question_with_no_colon(lang):
    from neoffice_branding import reply

    text = reply("transient_card", lang)
    assert text.endswith("?") and text.count("?") == 1, "the accounting chat keeps the questions of a block reason"
    assert ":" not in text, "the chat line already reads « ✋ <pole> : <question> »"


def test_the_real_dispatcher_tick_respawns_the_short_card_and_holds_the_quota_card(conn, clock, all_assignees_spawnable):
    """Through dispatch_once: two cards spawned, both end on a transient exit, a quota and a server error; 16 s later
    the tick spawns the server error's card again and holds the quota's: the cooldown is each card's own."""
    pids = []

    def spawn(task, workspace, board=None):
        pids.append(_dead_pid())
        return pids[-1]

    quota = kb.create_task(conn, title="a question", assignee="compta")
    short = kb.create_task(conn, title="another question", assignee="rh")
    kbd.dispatch_once(conn, spawn_fn=spawn)
    assert {kb.get_task(conn, t).status for t in (quota, short)} == {"running"} and len(pids) == 2
    for tid, reason in ((quota, "rate_limit"), (short, "server_error")):
        task = kb.get_task(conn, tid)
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "worker_result", {"pid": task.worker_pid, "claim_lock": task.claim_lock,
                             "exit_code": TRANSIENT, "last_output": "", "transient_reason": reason},
                             run_id=task.current_run_id)
    kbd.dispatch_once(conn, spawn_fn=spawn)  # reclaims both exits; both cooldowns hold
    assert [kb.get_task(conn, t).status for t in (quota, short)] == ["ready", "ready"] and len(pids) == 2
    clock(16)
    kbd.dispatch_once(conn, spawn_fn=spawn)
    assert [kb.get_task(conn, t).status for t in (quota, short)] == ["ready", "running"] and len(pids) == 3


def test_the_reason_is_read_on_the_dead_workers_own_payload(conn):
    """The second-exit check reads the dead worker the sweep builds, whose payload is a dict, not a run's JSON text.
    Read with a decoder of text it came back {}, and the second short transient in a row never blocked the card."""
    tid = kb.create_task(conn, title="a question", assignee="compta")
    _managed_exit(conn, tid, "server_error")  # the card's previous run: a short transient, closed
    dead = kbd._classify_dead_worker(_dead_pid(), "host:owner", TRANSIENT, task_id=tid)
    assert dead.rate_limited and isinstance(dead.event_payload, dict)
    dead.event_payload["transient_reason"] = "server_error"  # as the sweep records it
    assert kbd._neoffice_short_transient(dead.event_payload) is True
    assert kbd._neoffice_second_short_transient(conn, tid, dead) is True
    dead.event_payload["transient_reason"] = "rate_limit"
    assert kbd._neoffice_second_short_transient(conn, tid, dead) is False
