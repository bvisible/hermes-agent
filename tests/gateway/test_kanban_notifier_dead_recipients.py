# //// Neoffice — added file (no upstream equivalent): the notifier and dead WhatsApp
# //// recipients (#625).
"""On the development instance (2026-09-22), a worker finished its task and its user never got
the result: the kanban notifier spent its ticks on 13 subscriptions to numbers the central
WhatsApp router could not reach. Each attempt waited out the router's 15 s timeout, every
subscription was retried twelve times, and deliveries ran one after the other, so a live
answer queued behind them. The failure itself was logged empty ("deliver error: ")."""
import asyncio
import time

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import SendResult
from gateway.platforms.webhook import WebhookAdapter, _whatsapp_router_error_kind
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn

HANG_S = 0.5


def _runner(platform, adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {platform: adapter}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    return runner


async def _one_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


def _completed_task(conn, platform, chat_id):
    tid = kb.create_task(conn, title=f"task for {chat_id}", assignee="worker")
    kbn.add_notify_sub(conn, task_id=tid, platform=platform, chat_id=chat_id)
    kb.complete_task(conn, tid, summary="done")
    return tid


async def _wait(seconds):
    """A delay that does not go through asyncio.sleep, which the tick helper patches."""
    loop = asyncio.get_running_loop()
    done = loop.create_future()
    loop.call_later(seconds, done.set_result, None)
    await done


class HangingRouter:
    """The router hangs on dead numbers until the gateway's timeout, and delivers the live one."""

    def __init__(self):
        self.delivered = []

    async def send(self, chat_id, text, metadata=None):
        if chat_id.startswith("dead"):
            await _wait(HANG_S)
            return SendResult(success=False, error="whatsapp_router deliver timed out after 15 s",
                              error_kind="transient")
        self.delivered.append(chat_id)
        return SendResult(success=True)


class RejectingRouter:
    def __init__(self, kind):
        self.kind = kind
        self.attempts = 0

    async def send(self, chat_id, text, metadata=None):
        self.attempts += 1
        return SendResult(success=False, error="whatsapp_router deliver 400", error_kind=self.kind)


def test_a_recipient_that_hangs_no_longer_delays_the_others(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "hang.db"))
    kb.init_db()
    conn = kbc.connect()
    try:
        for i in range(10):
            _completed_task(conn, "telegram", f"dead-{i}")
        _completed_task(conn, "telegram", "chat-live")  # created last: delivered last in sequence
    finally:
        conn.close()
    router = HangingRouter()
    runner = _runner(Platform.TELEGRAM, router)
    started = time.monotonic()
    asyncio.run(_one_tick(monkeypatch, runner))
    elapsed = time.monotonic() - started
    assert router.delivered == ["chat-live"]
    # One after the other, the ten dead numbers alone take 10 x HANG_S = 5 s.
    assert elapsed < 4 * HANG_S, f"the tick took {elapsed:.1f} s"


def test_a_dead_whatsapp_number_is_dropped_at_its_first_answer(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "dead.db"))
    kb.init_db()
    conn = kbc.connect()
    try:
        tid = _completed_task(conn, "webhook", "whatsapp:+41000000000")
    finally:
        conn.close()
    router = RejectingRouter("not_found")
    asyncio.run(_one_tick(monkeypatch, _runner(Platform.WEBHOOK, router)))
    conn = kbc.connect()
    try:
        assert router.attempts == 1
        assert kbn.list_notify_subs(conn, tid) == []
    finally:
        conn.close()


def test_a_router_key_problem_drops_no_subscription(tmp_path, monkeypatch):
    """A 401/403 is our own key: every recipient fails alike, none of them is dead."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "key.db"))
    kb.init_db()
    conn = kbc.connect()
    try:
        tid = _completed_task(conn, "webhook", "whatsapp:+41000000001")
    finally:
        conn.close()
    router = RejectingRouter("forbidden")
    asyncio.run(_one_tick(monkeypatch, _runner(Platform.WEBHOOK, router)))
    conn = kbc.connect()
    try:
        assert router.attempts == 1
        assert len(kbn.list_notify_subs(conn, tid)) == 1
    finally:
        conn.close()


def test_the_router_answers_are_sorted_by_kind():
    assert _whatsapp_router_error_kind(404, "") == "not_found"
    assert _whatsapp_router_error_kind(400, '{"error": "phone is required (string, +E.164)"}') == "not_found"
    assert _whatsapp_router_error_kind(400, '{"error": "text exceeds 4096 chars"}') == "too_long"
    assert _whatsapp_router_error_kind(403, "") == "forbidden"
    assert _whatsapp_router_error_kind(400, '{"error": "text is required (string)"}') == "bad_format"
    assert _whatsapp_router_error_kind(503, '{"error": "whatsapp_not_connected"}') == "transient"


def test_a_timeout_says_what_it_is(monkeypatch):
    import aiohttp

    class _Post:
        async def __aenter__(self):
            raise asyncio.TimeoutError()

        async def __aexit__(self, *_):
            return False

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        def post(self, *_a, **_kw):
            return _Post()

    monkeypatch.setattr(aiohttp, "ClientSession", lambda *a, **kw: _Session())
    adapter = WebhookAdapter(PlatformConfig(enabled=True, extra={"routes": {}}))
    delivery = {"deliver_extra": {"router_url": "http://router.test", "router_api_key": "k",
                                  "phone": "+41000000002"}}
    result = asyncio.run(adapter._deliver_whatsapp_router("bonjour", delivery))
    assert result.success is False
    assert result.error == "whatsapp_router deliver timed out after 15 s"
    assert result.error_kind == "transient"
