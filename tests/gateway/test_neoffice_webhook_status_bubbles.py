# //// Neoffice — added file (no upstream equivalent): interim status bubbles never reach NORA's
# //// customer chats, and every other webhook route keeps upstream's raw status.
"""Where a status callback ends on the webhook platform.

On 2026-09-01 an inference engine answered 503 and the gateway sent « ⏳ Retrying in 2.9s
(attempt 1/3)... » to the desk: the Quick Chat took that bubble for NORA's reply, stopped polling,
and the worker's answer, 70 s later, was never shown. Until 08.10 the fork dropped every status on
the webhook platform inside ``_prepare_gateway_status_message``, which broke upstream's contract
that a programmatic webhook keeps its raw diagnostics (two upstream tests). The filter now sits in
the webhook adapter and drops only what a nora or whatsapp_router delivery would receive.
"""
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.base import SendResult
from gateway.platforms.webhook import _INSECURE_NO_AUTH, WebhookAdapter
from gateway.run import _prepare_gateway_status_message, _send_or_update_status_coro

RETRY = "⏳ Retrying in 2.9s (attempt 1/3)..."
DESK = "webhook:nora_chat:user:qa@example.com"
WHATSAPP = "webhook:whatsapp_inbox:phone:+41790000000"
GITHUB = "webhook:github:delivery:1"


def _adapter():
    routes = {
        "nora_chat": {"secret": _INSECURE_NO_AUTH, "prompt": "{message}", "deliver": "nora",
                      "deliver_extra": {"conversation_id": "{conversation_id}"}},
        "whatsapp_inbox": {"secret": _INSECURE_NO_AUTH, "prompt": "{message}", "deliver": "whatsapp_router",
                           "deliver_extra": {"phone": "{phone}"}},
        "github": {"secret": _INSECURE_NO_AUTH, "prompt": "{message}", "deliver": "github_comment",
                   "deliver_extra": {"repo": "example/repo", "pr_number": "1"}},
    }
    config = PlatformConfig(enabled=True, extra={"host": "0.0.0.0", "port": 0, "routes": routes,
                                                 "rate_limit": 30, "max_body_bytes": 1_048_576})
    adapter = WebhookAdapter(config)
    adapter._deliver_nora = AsyncMock(return_value=SendResult(success=True))
    adapter._deliver_whatsapp_router = AsyncMock(return_value=SendResult(success=True))
    adapter._deliver_github_comment = AsyncMock(return_value=SendResult(success=True))
    return adapter


def _remember(adapter, chat_id, route):
    adapter._delivery_info[chat_id] = {"deliver": adapter._routes[route]["deliver"], "route": route,
                                       "deliver_extra": dict(adapter._routes[route]["deliver_extra"])}


def test_the_turn_hands_the_raw_status_to_the_adapter_as_upstream_does():
    assert _prepare_gateway_status_message("webhook", "lifecycle", RETRY) == RETRY


@pytest.mark.asyncio
@pytest.mark.parametrize("chat_id, route, channel", ((DESK, "nora_chat", "_deliver_nora"),
                                                     (WHATSAPP, "whatsapp_inbox", "_deliver_whatsapp_router")))
async def test_a_customer_chat_never_receives_a_status(chat_id, route, channel):
    adapter = _adapter()
    _remember(adapter, chat_id, route)
    result = await _send_or_update_status_coro(adapter, chat_id, "lifecycle", RETRY, None)
    assert result.success is True
    getattr(adapter, channel).assert_not_awaited()


@pytest.mark.asyncio
async def test_the_reply_itself_still_reaches_the_desk():
    adapter = _adapter()
    _remember(adapter, DESK, "nora_chat")
    await adapter.send(DESK, "Vous avez 25 factures en retard.")
    adapter._deliver_nora.assert_awaited_once()
    assert adapter._deliver_nora.await_args.args[0] == "Vous avez 25 factures en retard."


@pytest.mark.asyncio
async def test_a_programmatic_route_keeps_upstreams_raw_status():
    adapter = _adapter()
    _remember(adapter, GITHUB, "github")
    await _send_or_update_status_coro(adapter, GITHUB, "lifecycle", RETRY, None)
    adapter._deliver_github_comment.assert_awaited_once()
    assert adapter._deliver_github_comment.await_args.args[0] == RETRY
# //// END Neoffice ////
