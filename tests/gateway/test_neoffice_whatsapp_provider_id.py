# //// Neoffice — added file (no upstream equivalent): the WhatsApp inbound route deduplicates
# //// on the provider's message id (fleet tracker #455).
"""A retried WhatsApp delivery is recognised as the same message.

The central WhatsApp router sends no delivery header, so the gateway used its own millisecond
clock as the delivery id: each router retry (after a 5xx or a refused connection, same payload
bytes) looked new and could start a second run and a second kanban task. The router forwards
the provider's message id as ``message_id``; on ``whatsapp_inbox`` it is now the delivery id.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.webhook import (
    _INSECURE_NO_AUTH,
    WebhookAdapter,
    _neoffice_provider_delivery_id,
)


def _adapter(route_name):
    routes = {route_name: {"secret": _INSECURE_NO_AUTH, "prompt": "{message}"}}
    config = PlatformConfig(enabled=True, extra={"host": "0.0.0.0", "port": 0, "routes": routes,
                                                 "rate_limit": 30, "max_body_bytes": 1_048_576})
    adapter = WebhookAdapter(config)
    adapter.handle_message = AsyncMock()
    return adapter


def _app(adapter):
    from aiohttp import web

    app = web.Application(client_max_size=adapter._max_body_bytes)
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)
    return app


def _routed_router():
    """The NORA pre-router, replaced: it records each call and says the task was created."""
    return MagicMock(return_value={"routed": True, "category": "ventes", "task_id": "t_0a1b2c3d"})


# ── the id itself ──────────────────────────────────────────────────────────────────────────

def test_the_provider_id_becomes_the_delivery_id_on_the_whatsapp_route():
    got = _neoffice_provider_delivery_id("whatsapp_inbox", {"message_id": "3EB0C767D2AB"}, "1727282828000")
    assert got == "whatsapp_inbox:3EB0C767D2AB"


@pytest.mark.parametrize("payload", ({}, {"message_id": ""}, {"message_id": "   "}, {"message_id": None},
                                     {"message_id": 1234}, "not a dict"))
def test_without_a_usable_provider_id_the_delivery_id_stands(payload):
    assert _neoffice_provider_delivery_id("whatsapp_inbox", payload, "req-1") == "req-1"


def test_another_route_keeps_its_delivery_id():
    assert _neoffice_provider_delivery_id("nora_chat", {"message_id": "3EB0"}, "req-1") == "req-1"


# ── through the real handler ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_router_retry_of_the_same_message_starts_one_run():
    adapter = _adapter("whatsapp_inbox")
    router = _routed_router()
    body = {"phone": "+41790000000", "message": "Bonjour", "message_id": "3EB0C767D2AB"}
    with patch("gateway.nora_chat_router.route_chat_message", router):
        async with TestClient(TestServer(_app(adapter))) as cli:
            first = await cli.post("/webhooks/whatsapp_inbox", json=body)
            await asyncio.sleep(0.005)  # the clock fallback now differs: only the message id can match
            retry = await cli.post("/webhooks/whatsapp_inbox", json=body)  # no delivery header, like the router
            assert first.status == 202
            assert retry.status == 200 and (await retry.json())["status"] == "duplicate"
    assert router.call_count == 1
    assert router.call_args.kwargs["idempotency_key"] == "whatsapp_inbox:3EB0C767D2AB"


@pytest.mark.asyncio
async def test_the_provider_id_wins_over_a_delivery_header():
    adapter = _adapter("whatsapp_inbox")
    router = _routed_router()
    body = {"phone": "+41790000000", "message": "Bonjour", "message_id": "3EB0C767D2AB"}
    with patch("gateway.nora_chat_router.route_chat_message", router):
        async with TestClient(TestServer(_app(adapter))) as cli:
            first = await cli.post("/webhooks/whatsapp_inbox", json=body, headers={"X-Request-ID": "req-1"})
            retry = await cli.post("/webhooks/whatsapp_inbox", json=body, headers={"X-Request-ID": "req-2"})
            assert first.status == 202
            assert retry.status == 200 and (await retry.json())["status"] == "duplicate"
    assert router.call_count == 1


@pytest.mark.asyncio
async def test_two_different_messages_are_two_runs():
    adapter = _adapter("whatsapp_inbox")
    router = _routed_router()
    with patch("gateway.nora_chat_router.route_chat_message", router):
        async with TestClient(TestServer(_app(adapter))) as cli:
            a = await cli.post("/webhooks/whatsapp_inbox", json={"phone": "+41790000000", "message": "un",
                                                                  "message_id": "AAA1"})
            b = await cli.post("/webhooks/whatsapp_inbox", json={"phone": "+41790000000", "message": "un",
                                                                  "message_id": "AAA2"})
            assert a.status == 202 and b.status == 202
    assert router.call_count == 2


@pytest.mark.asyncio
async def test_without_message_id_the_header_delivery_id_is_used_as_before():
    adapter = _adapter("whatsapp_inbox")
    router = _routed_router()
    body = {"phone": "+41790000000", "message": "Bonjour"}
    with patch("gateway.nora_chat_router.route_chat_message", router):
        async with TestClient(TestServer(_app(adapter))) as cli:
            first = await cli.post("/webhooks/whatsapp_inbox", json=body, headers={"X-Request-ID": "req-1"})
            again = await cli.post("/webhooks/whatsapp_inbox", json=body, headers={"X-Request-ID": "req-1"})
            assert first.status == 202
            assert again.status == 200 and (await again.json())["status"] == "duplicate"
    assert router.call_count == 1
    assert router.call_args.kwargs["idempotency_key"] == "req-1"


@pytest.mark.asyncio
async def test_another_route_ignores_a_message_id_in_its_payload():
    adapter = _adapter("github_events")
    body = {"message": "push", "message_id": "same-id"}
    async with TestClient(TestServer(_app(adapter))) as cli:
        a = await cli.post("/webhooks/github_events", json=body, headers={"X-Request-ID": "req-a"})
        b = await cli.post("/webhooks/github_events", json=body, headers={"X-Request-ID": "req-b"})
        assert a.status == 202 and b.status == 202
