# //// Neoffice — added file (no upstream equivalent): the voice of a WhatsApp voice reply is the instance's own, tts.noraai.ch.
"""A person who sends Nora a voice note on WhatsApp gets her reply as a voice note (NORA #30). It was said by edge-tts, a
Microsoft service, in another voice than every other way Nora speaks. Since 18.11.2026 the gateway asks the instance's own
TTS (tts.noraai.ch), with the call the Nora app makes (``POST /synthesize``, ``{text, voice, format: mp3}``), its address
and key read from the gateway's ``tts.openai`` settings, else from NORA_TTS_URL and OLARES_API_KEY. Whatever goes wrong,
the reply goes as text. A stand-in TTS and a stand-in WhatsApp router on this machine answer; nothing leaves it (edge-tts
is replaced by a module that counts its calls, and every test checks it was never called)."""

import asyncio
import base64
import socket
import sys
import types

import pytest
from aiohttp import web

from gateway.config import PlatformConfig
from gateway.platforms.webhook import WebhookAdapter

MP3 = b"ID3\x04\x00\x00" + b"\xff\xfb" * 400
EDGE_CALLS = []


@pytest.fixture(autouse=True)
def _no_edge_tts(monkeypatch):
    """edge-tts must never be called: a stand-in module counts its calls, and none may happen."""
    EDGE_CALLS.clear()

    def _communicate(*a, **k):
        EDGE_CALLS.append((a, k))
        raise RuntimeError("edge-tts is not reachable from a test")

    monkeypatch.setitem(sys.modules, "edge_tts", types.SimpleNamespace(Communicate=_communicate))
    for name in ("NORA_TTS_URL", "OLARES_API_KEY", "WHATSAPP_TTS_VOICE", "WHATSAPP_TTS_TIMEOUT", "WHATSAPP_TTS_MAX_CHARS"):
        monkeypatch.delenv(name, raising=False)
    yield
    assert EDGE_CALLS == [], "edge-tts was called"


def _settings(monkeypatch, openai):
    monkeypatch.setattr("hermes_cli.config.load_config", lambda *a, **k: {"tts": {"provider": "openai", "openai": openai}})


def _closed_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run(monkeypatch, openai, env=None, deliver=False, **kw):
    """A stand-in TTS (and WhatsApp router) on 127.0.0.1. The settings and the environment take its port through {port}.
    -> (what the synthesis or the delivery returned, the TTS requests, the router requests)."""
    loop = asyncio.new_event_loop()

    async def go():
        tts_seen, router_seen = [], []

        async def synthesize(request):
            tts_seen.append({"path": request.path, "json": await request.json(), "auth": request.headers.get("Authorization")})
            return web.Response(
                status=kw.get("status", 200), body=kw.get("body", MP3), content_type=kw.get("content_type", "audio/mpeg")
            )

        async def router(request):
            router_seen.append({"path": request.path, "json": await request.json(), "auth": request.headers.get("Authorization")})
            return web.json_response({"ok": True})

        app = web.Application()
        app.router.add_post("/synthesize", synthesize)
        app.router.add_post("/api/sendAudio", router)
        app.router.add_post("/api/sendText", router)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        if openai is not None:
            _settings(monkeypatch, {k: (v.replace("{port}", str(port)) if isinstance(v, str) else v) for k, v in openai.items()}
                      if isinstance(openai, dict) else openai)
        for name, value in (env or {}).items():
            monkeypatch.setenv(name, value.replace("{port}", str(port)))
        try:
            adapter = WebhookAdapter(PlatformConfig(enabled=True, extra={"routes": {}}))
            text = kw.get("text", "Bonjour, votre facture est prête.")
            if deliver:
                extra = {"router_url": f"http://127.0.0.1:{port}", "router_api_key": "the-router-key",
                         "phone": "+41790000000", "is_audio": "true"}
                result = await adapter._deliver_whatsapp_router(text, {"deliver_extra": extra})
            else:
                result = await adapter._synthesize_voice_b64(text)
            return result, tts_seen, router_seen
        finally:
            await runner.cleanup()

    try:
        return loop.run_until_complete(go())
    finally:
        loop.close()


OK_SETTINGS = {"base_url": "http://127.0.0.1:{port}/v1", "api_key": "the-instance-key", "voice": "estelle"}


def test_a_voice_reply_is_said_by_the_instance_tts_with_the_nora_app_call(monkeypatch):
    result, tts, _router = _run(monkeypatch, OK_SETTINGS)
    assert result is not None and base64.b64decode(result) == MP3
    assert len(tts) == 1
    assert tts[0]["path"] == "/synthesize"
    assert tts[0]["json"] == {"text": "Bonjour, votre facture est prête.", "voice": "fr_female", "format": "mp3"}
    assert tts[0]["auth"] == "Bearer the-instance-key"


@pytest.mark.parametrize("base_url", ["http://127.0.0.1:{port}/v1/", "http://127.0.0.1:{port}", "http://127.0.0.1:{port}/"])
def test_the_settings_address_works_with_or_without_the_openai_root(monkeypatch, base_url):
    result, tts, _router = _run(monkeypatch, {"base_url": base_url, "api_key": "k"})
    assert result is not None
    assert [r["path"] for r in tts] == ["/synthesize"]


def test_the_gateway_settings_come_before_the_environment(monkeypatch):
    result, tts, _router = _run(
        monkeypatch, OK_SETTINGS, env={"NORA_TTS_URL": f"http://127.0.0.1:{_closed_port()}", "OLARES_API_KEY": "the-env-key"}
    )
    assert result is not None
    assert tts and tts[0]["auth"] == "Bearer the-instance-key"


@pytest.mark.parametrize("openai", [{}, "a scalar where a mapping belongs"])
def test_without_usable_settings_the_address_and_key_of_the_environment_are_used(monkeypatch, openai):
    result, tts, _router = _run(monkeypatch, openai, env={"NORA_TTS_URL": "http://127.0.0.1:{port}", "OLARES_API_KEY": "the-env-key"})
    assert result is not None and base64.b64decode(result) == MP3
    assert tts and tts[0]["auth"] == "Bearer the-env-key"


def test_the_voice_can_be_chosen(monkeypatch):
    _result, tts, _router = _run(monkeypatch, OK_SETTINGS, env={"WHATSAPP_TTS_VOICE": "fr_male"})
    assert tts and tts[0]["json"]["voice"] == "fr_male"


def test_a_timeout_that_does_not_parse_keeps_the_default(monkeypatch):
    result, tts, _router = _run(monkeypatch, OK_SETTINGS, env={"WHATSAPP_TTS_TIMEOUT": "soon"})
    assert result is not None and len(tts) == 1


def test_no_key_means_a_text_reply_and_no_call(monkeypatch):
    result, tts, _router = _run(monkeypatch, {}, env={"NORA_TTS_URL": "http://127.0.0.1:{port}"})
    assert result is None
    assert tts == []


@pytest.mark.parametrize(
    "answer",
    [
        {"status": 503, "body": b'{"detail":"busy"}', "content_type": "application/json"},
        {"status": 200, "body": b"RIFF" + b"\x00" * 400, "content_type": "audio/wav"},  # the router is told audio/mpeg
        {"status": 200, "body": b"ID3", "content_type": "audio/mpeg"},  # an empty sound
    ],
)
def test_a_tts_that_fails_means_a_text_reply(monkeypatch, answer):
    result, tts, _router = _run(monkeypatch, OK_SETTINGS, **answer)
    assert result is None
    assert len(tts) == 1


def test_a_tts_that_cannot_be_reached_means_a_text_reply(monkeypatch):
    monkeypatch.setenv("WHATSAPP_TTS_TIMEOUT", "5")
    _settings(monkeypatch, {"base_url": f"http://127.0.0.1:{_closed_port()}/v1", "api_key": "k"})
    adapter = WebhookAdapter(PlatformConfig(enabled=True, extra={"routes": {}}))
    loop = asyncio.new_event_loop()
    try:
        assert loop.run_until_complete(adapter._synthesize_voice_b64("Bonjour.")) is None
    finally:
        loop.close()


# ---- the whole delivery: the voice note goes to the router, and any failure of the voice leaves the text reply


def test_the_delivery_sends_the_voice_note_to_the_router(monkeypatch):
    result, tts, router = _run(monkeypatch, OK_SETTINGS, deliver=True)
    assert result.success
    assert len(tts) == 1
    assert [r["path"] for r in router] == ["/api/sendAudio"]
    sent = router[0]["json"]
    assert sent["mimetype"] == "audio/mpeg" and base64.b64decode(sent["audio_base64"]) == MP3
    assert sent["phone"] == "+41790000000" and sent["caption"] == "Bonjour, votre facture est prête."
    assert router[0]["auth"] == "Bearer the-router-key"


@pytest.mark.parametrize(
    "case",
    [
        {"status": 503, "body": b'{"detail":"busy"}', "content_type": "application/json"},  # the TTS refuses
        {"env": {"WHATSAPP_TTS_MAX_CHARS": "lots"}},  # a setting that does not parse raises inside the synthesis
    ],
)
def test_a_voice_that_fails_leaves_the_text_reply(monkeypatch, case):
    case = dict(case)
    result, _tts, router = _run(monkeypatch, OK_SETTINGS, env=case.pop("env", None), deliver=True, **case)
    assert result.success
    assert [r["path"] for r in router] == ["/api/sendText"]
    assert router[0]["json"] == {"phone": "+41790000000", "text": "Bonjour, votre facture est prête."}
