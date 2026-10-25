# //// Neoffice — added file (no upstream equivalent): a test's own WhatsApp router, on this machine only.
"""The latency bench (hermes-poc tests/llm/22) posts real messages on the whatsapp_inbox route with made-up
numbers. Their replies went to the central WhatsApp router on the hub, which waited 15 s on WhatsApp for each
one (twelve attempts per worker result). The route declares ``bench_router_url: '{bench_router_url}'``: a
payload that gives an http address of this machine has its replies sent there, without the central router's
key; anything else, and above all a payload of the real router (which has no such field), changes nothing."""

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.webhook import WebhookAdapter, _neoffice_bench_router
from gateway.platforms.webhook_delivery import validate_destination

ROUTE_EXTRA = {
    "router_url": "https://hub.example/whatsapp-router",
    "router_api_key": "the-central-router-key",
    "phone": "{phone}",
    "is_audio": "{is_audio}",
    "bench_router_url": "{bench_router_url}",
}


def _render(payload):
    adapter = WebhookAdapter(PlatformConfig(enabled=True, extra={"routes": {}}))
    return adapter._render_delivery_extra(dict(ROUTE_EXTRA), payload)


def test_a_payload_of_the_real_router_keeps_the_central_router():
    extra = _render({"phone": "+41790000000", "is_audio": False})
    assert extra["router_url"] == "https://hub.example/whatsapp-router"
    assert extra["router_api_key"] == "the-central-router-key"
    assert extra["phone"] == "+41790000000"


@pytest.mark.parametrize("url", ["http://127.0.0.1:43123", "http://127.0.0.1:43123/", "http://localhost:9", "http://[::1]:80"])
def test_a_loopback_bench_router_takes_the_replies_without_the_central_key(url):
    extra = _render({"phone": "+417000800M000", "is_audio": False, "bench_router_url": url})
    assert extra["router_url"] == url.rstrip("/")
    assert extra["router_api_key"] == "bench-router"


@pytest.mark.parametrize(
    "url",
    [
        "http://evil.example:80",  # not this machine
        "https://127.0.0.1:443",  # not the plain http a local stand-in serves
        "http://user:pass@127.0.0.1:8080",  # credentials in the address
        "http://127.0.0.1.evil.example",  # a name that only starts like loopback
        "{bench_router_url}",  # the template left as is
        "",
    ],
)
def test_anything_but_a_plain_loopback_address_changes_nothing(url):
    extra = _render({"phone": "+41790000000", "bench_router_url": url})
    assert extra["router_url"] == "https://hub.example/whatsapp-router"
    assert extra["router_api_key"] == "the-central-router-key"


def test_an_extra_without_the_field_is_returned_as_is():
    extra = {"router_url": "https://hub.example/whatsapp-router", "router_api_key": "k", "phone": "+41"}
    assert _neoffice_bench_router(extra) is extra


def test_the_queue_keeps_the_bench_address_and_never_the_central_key():
    extra = _render({"phone": "+417000800M000", "is_audio": False, "bench_router_url": "http://127.0.0.1:43123"})
    kept = validate_destination({"deliver": "whatsapp_router", "deliver_extra": extra})["deliver_extra"]
    assert kept == {"phone": "+417000800M000", "is_audio": "False", "bench_router_url": "http://127.0.0.1:43123"}
