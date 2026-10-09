# //// Neoffice — added file (no upstream equivalent): a slow destination never holds the next notifier tick.
"""One recipient that hangs must not delay the answers of everybody else (09.10).

On the development instance, the WhatsApp router timed out (15 s, twelve attempts) on the results of two test
tasks; a desk answer completed at 05:31:15 reached its chat at 05:31:31. Deliveries of one tick already ran
concurrently (#625), but the tick waited for all of them before the next collect, so every result that completed
meanwhile waited behind the slowest send. A tick now waits at most NOTIFIER_TICK_WAIT_S; a delivery still running
goes on in the background, its destination is left unclaimed until it ends (one chat keeps its order and a rewind
never meets a newer claim), and the watcher drains what is left before it returns.
"""
import asyncio
import time

from gateway import kanban_watchers as KW
from gateway.config import Platform
from gateway.kanban_watchers_notifier import _notifier_collect
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn

SLOW_S = 3.0


_real_sleep = asyncio.sleep  # the tests below replace asyncio.sleep to drive the watcher's ticks


class SlowChatAdapter:
    """Records each send with its time; the chat « slow-chat » takes SLOW_S to answer."""

    def __init__(self):
        self.sent = []

    async def send(self, chat_id, text, metadata=None):
        if chat_id == "slow-chat":
            await _real_sleep(SLOW_S)
        self.sent.append((chat_id, text, time.monotonic()))

    async def handle_message(self, event):
        event._gateway_accepted = True


def _make_runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    return runner


def _task_with_sub(chat_id, title, complete=True):
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title=title, assignee="worker")
        kbn.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id=chat_id)
        if complete:
            kb.complete_task(conn, tid, summary=f"{title} done")
        return tid
    finally:
        conn.close()


def _complete(tid, summary):
    conn = kbc.connect()
    try:
        kb.complete_task(conn, tid, summary=summary)
    finally:
        conn.close()


def _run_watcher(monkeypatch, runner, between_ticks):
    """Run the real notifier loop; *between_ticks(n)* runs at the n-th wait and returns False to stop."""
    ticks = {"n": 0}

    async def fake_sleep(delay):
        if delay == 5:  # the watcher's start-up delay
            return None
        ticks["n"] += 1
        if not between_ticks(ticks["n"]):
            runner._running = False
        await _real_sleep(0.05)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(KW, "KANBAN_NOTIFIER_HOT_UNTIL", float("-inf"))
    started = time.monotonic()
    asyncio.run(runner._kanban_notifier_watcher(interval=1))
    return started


def test_an_answer_does_not_wait_for_a_slow_destination(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "slow.db"))
    kb.init_db()
    _task_with_sub("slow-chat", "dead whatsapp number")
    fast = _task_with_sub("desk-chat", "desk answer", complete=False)
    adapter = SlowChatAdapter()
    runner = _make_runner(adapter)
    done_at = {}

    def between(n):
        if n == 1:  # the desk answer completes while the slow send is still running
            _complete(fast, "the desk answer")
            done_at["fast"] = time.monotonic()
        return n < 3

    _run_watcher(monkeypatch, runner, between)
    sent = {chat: at for chat, _, at in adapter.sent}
    assert set(sent) == {"slow-chat", "desk-chat"}  # the watcher drained the slow send before returning
    assert sent["desk-chat"] < sent["slow-chat"]
    assert sent["desk-chat"] - done_at["fast"] < 2.0


def test_one_chat_keeps_its_order_behind_a_slow_send(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "order.db"))
    kb.init_db()
    _task_with_sub("slow-chat", "first answer")
    second = _task_with_sub("slow-chat", "second answer", complete=False)
    adapter = SlowChatAdapter()
    runner = _make_runner(adapter)

    def between(n):
        if n == 1:
            _complete(second, "second answer")
        return len(adapter.sent) < 2 and n < 200

    _run_watcher(monkeypatch, runner, between)
    texts = [text for chat, text, _ in adapter.sent if chat == "slow-chat"]
    assert len(texts) == 2
    assert "first answer" in texts[0] and "second answer" in texts[1]


def test_a_busy_destination_is_left_unclaimed(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "busy.db"))
    kb.init_db()
    tid = _task_with_sub("chat-1", "waiting")
    runner = _make_runner(SlowChatAdapter())
    busy = frozenset({("telegram", "chat-1", "")})
    assert _notifier_collect(runner, kb, notifier_profile=None, gc_due=False, gc_retention_days=30, busy=busy) == []
    conn = kbc.connect()
    try:
        _, events = kbn.unseen_events_for_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1",
                                              kinds=["completed"])
    finally:
        conn.close()
    assert events  # nothing was claimed: the cursor did not move
    collected = _notifier_collect(runner, kb, notifier_profile=None, gc_due=False, gc_retention_days=30)
    assert [d["sub"]["chat_id"] for d in collected] == ["chat-1"]
