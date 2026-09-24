# //// Neoffice — added file (no upstream equivalent): NORA's memory events must not
# //// spend the chat route's rate limit.
"""Every night at 22:30 NORA's memory consolidation POSTs memory_read/retain/forget to the
chat route. They counted in the same 30/min as people's messages: on the dev instance
(2026-09-24 22:33) two chat messages were refused with 429 and the desk showed « service
saturé ». Memory events now count in a bucket of their own, which keeps their pacing."""
import hashlib
import hmac
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.webhook import WebhookAdapter

SECRET = "test-secret-key"
ROUTE = "chat-route"
RATE_LIMIT = 3


def _adapter(**extra) -> WebhookAdapter:
    routes = {ROUTE: {"secret": SECRET, "events": ["push"], "prompt": "Event: {event}", "deliver": "log"}}
    config = PlatformConfig(enabled=True, extra={"host": "0.0.0.0", "port": 0, "routes": routes,
                                                 "rate_limit": RATE_LIMIT, "memory_rate_limit": RATE_LIMIT, **extra})
    return WebhookAdapter(config)


def _signed(payload: dict, delivery: str, event: str = "") -> tuple[bytes, dict]:
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json", "X-GitHub-Delivery": delivery,
               "X-Hub-Signature-256": "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()}
    if event:
        headers["X-GitHub-Event"] = event
    return body, headers


@pytest.mark.asyncio
async def test_a_memory_burst_does_not_refuse_a_chat_message():
    adapter = _adapter()
    reads = []

    async def fake_read(payload):
        reads.append(payload)
        return web.json_response({"status": "ok"})

    async def capture(_event):
        return None

    adapter._handle_memory_read = fake_read
    adapter.handle_message = capture
    app = web.Application()
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)

    async with TestClient(TestServer(app)) as cli:
        for i in range(RATE_LIMIT):
            body, headers = _signed({"event_type": "memory_read", "user": "u@example.test"}, f"mem-{i}")
            assert (await cli.post(f"/webhooks/{ROUTE}", data=body, headers=headers)).status == 200
        body, headers = _signed({"event_type": "memory_read", "user": "u@example.test"}, "mem-over")
        assert (await cli.post(f"/webhooks/{ROUTE}", data=body, headers=headers)).status == 429, (
            "memory keeps its own pacing: the consolidation backs off on 429")

        body, headers = _signed({"event": "push", "data": "a person's message"}, "chat-1", event="push")
        resp = await cli.post(f"/webhooks/{ROUTE}", data=body, headers=headers)
        assert resp.status == 202, f"a chat message was refused by the memory burst ({resp.status})"
    assert len(reads) == RATE_LIMIT


def test_the_memory_bucket_is_eight_times_the_chat_pace_by_default():
    """A nightly pass makes up to ~40 calls per person: at the chat's pace it held a long
    worker for half an hour on the dev instance."""
    routes = {ROUTE: {"secret": SECRET, "events": ["push"], "prompt": "p", "deliver": "log"}}
    adapter = WebhookAdapter(PlatformConfig(enabled=True, extra={"routes": routes, "rate_limit": 30}))
    assert adapter._memory_rate_limit == 240
    assert all(adapter._record_rate_limit_hit("r::memory", 0.0, 240) for _ in range(240))
    assert not adapter._record_rate_limit_hit("r::memory", 0.0, 240)
    assert adapter._record_rate_limit_hit("r", 0.0), "the chat bucket is untouched"

