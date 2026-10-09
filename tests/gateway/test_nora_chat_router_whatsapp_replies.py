# //// Neoffice — added file (no upstream equivalent): the router's own replies reach WhatsApp (09.10).
"""A reply the pre-router writes itself (a greeting, a fast answer, « Pour quand ? ») reaches a WhatsApp chat.

The router posted every reply of its own to the desk callback only. A WhatsApp chat has none: its route
carries the central WhatsApp router (router_url, router_api_key, phone), so the reply was logged
« delivered=False » and, the turn being handled, the agent was skipped: the person got nothing (gateway log
of the dev instance, whatsapp_inbox). And a reminder asked without its moment kept its question under the
desk thread only, so « demain à 10h » sent on WhatsApp completed nothing.
"""
import io
import json
import sys
import types
import urllib.error
from types import SimpleNamespace

import pytest

from gateway import nora_chat_router as R

_ROUTER = "https://hub.example.test/whatsapp-router"
_PHONE = "+41790000000"
_WHATSAPP = {"router_url": _ROUTER, "router_api_key": "router-key", "phone": _PHONE, "is_audio": "false"}
_CB = "https://erp.example.test/api/method/nora.api.v2.hermes_callback.deliver"
_DESK = {"callback_url": _CB, "callback_token": "tok", "conversation_id": "conv-desk"}


class _Resp:
    status = 200

    def __init__(self, body=b'{"message": "ok"}'):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return self._body


@pytest.fixture
def posts(monkeypatch):
    import urllib.request

    seen = []

    def urlopen(req, timeout=None):
        seen.append(SimpleNamespace(url=req.full_url, headers={k.lower(): v for k, v in req.header_items()},
                                    body=json.loads(req.data.decode())))
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return seen


def test_a_reply_reaches_whatsapp_through_the_central_router(posts):
    assert R._post_ack_to_callback("Bonjour, que puis-je faire pour vous ?", dict(_WHATSAPP)) is True
    assert len(posts) == 1, posts
    assert posts[0].url == _ROUTER + "/api/sendText"
    assert posts[0].headers["authorization"] == "Bearer router-key"
    assert posts[0].body == {"phone": _PHONE, "text": "Bonjour, que puis-je faire pour vous ?"}


def test_the_desk_callback_is_used_when_there_is_one(posts):
    assert R._post_ack_to_callback("Bonjour", {**_WHATSAPP, **_DESK}) is True
    assert [p.url for p in posts] == [_CB]
    assert posts[0].body == {"conversation_id": "conv-desk", "text": "Bonjour"}


@pytest.mark.parametrize("extra", [
    {},
    {"router_url": _ROUTER, "phone": _PHONE},                              # no key
    {"router_url": _ROUTER, "router_api_key": "router-key"},               # no number
    {"router_url": _ROUTER, "router_api_key": "router-key", "phone": "{phone}"},  # template not rendered
])
def test_nothing_is_posted_without_a_whole_route(posts, extra):
    assert R._post_ack_to_callback("Bonjour", extra) is False
    assert posts == []


def test_a_refusal_of_the_whatsapp_router_is_not_a_delivery(monkeypatch):
    import urllib.request

    def urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 500, "Internal Server Error", None, None)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    assert R._post_ack_to_callback("Bonjour", dict(_WHATSAPP)) is False


@pytest.fixture
def whatsapp_chat(monkeypatch, posts):
    for d in (R._LAST_ROUTE, R._PENDING_SPACE, R._PENDING_NOTE, R._CONV_HISTORY, R._SPACE_ASKED,
              R._PENDING_REMINDER):
        d.clear()
    classified, reminders = [], []

    def fake_classify(message, **kw):
        classified.append(message)
        return "support"

    def fake_route_reminder(message, chat_user, deliver_extra):
        reminders.append(message)
        return {"routed": False, "category": "DIRECT", "ack": None, "task_id": None}  # no desk: declined

    monkeypatch.setattr(R, "_asks_fast_answer", lambda *a, **k: False)
    monkeypatch.setattr(R, "classify", fake_classify)
    monkeypatch.setattr(R, "_route_reminder", fake_route_reminder)
    kanban_db = types.SimpleNamespace(create_task=lambda conn, **kw: "t_whatsapp")
    connect = types.SimpleNamespace(connect=lambda board=None: SimpleNamespace(close=lambda: None))
    notify = types.SimpleNamespace(add_notify_sub=lambda *a, **kw: None)
    pkg = sys.modules.get("hermes_cli") or types.ModuleType("hermes_cli")
    monkeypatch.setitem(sys.modules, "hermes_cli", pkg)
    for name, mod in (("kanban_db", kanban_db), ("kanban_db_connect", connect), ("kanban_db_notify", notify)):
        monkeypatch.setattr(pkg, name, mod, raising=False)
        monkeypatch.setitem(sys.modules, f"hermes_cli.{name}", mod)

    def route(message):
        # The WhatsApp payload carries no conversation_id: the chat is the number (session_key: phone).
        return R.route_chat_message(
            message=message, session_chat_id=f"webhook:whatsapp_inbox:phone:{_PHONE}", conversation_id=None,
            thread_id=None, user_id=_PHONE, notifier_profile="default", idempotency_key="k",
            call_llm_fn=lambda **_kw: None, main_runtime=None, deliver_extra=dict(_WHATSAPP),
            chat_user="staff@example.test", language="fr", chat_phone=_PHONE,
        )

    yield SimpleNamespace(route=route, posts=posts, classified=classified, reminders=reminders)
    for d in (R._LAST_ROUTE, R._PENDING_SPACE, R._PENDING_NOTE, R._CONV_HISTORY, R._SPACE_ASKED,
              R._PENDING_REMINDER):
        d.clear()


def test_a_greeting_on_whatsapp_gets_its_answer(whatsapp_chat):
    result = whatsapp_chat.route("Bonjour")
    assert result["routed"] is True and result["ack_delivered"] is True, result
    assert [p.body["text"] for p in whatsapp_chat.posts] == [result["ack"]]


def test_the_reminder_moment_is_asked_and_completed_on_whatsapp(whatsapp_chat):
    first = whatsapp_chat.route("Rappelle-moi d'appeler le fournisseur de carrelage.")
    assert first["ack"] == R._REMINDER_WHEN["fr"] and first["ack_delivered"] is True, first
    assert [p.body for p in whatsapp_chat.posts] == [{"phone": _PHONE, "text": R._REMINDER_WHEN["fr"]}]

    second = whatsapp_chat.route("Demain à 10h.")
    assert whatsapp_chat.reminders == ["Rappelle-moi d'appeler le fournisseur de carrelage Demain à 10h."]
    # nora's route needs the desk callback, so on WhatsApp the orchestrator sets it, told the whole request.
    assert second["routed"] is False and "Demain à 10h" in second["agent_hint"], second
    assert whatsapp_chat.classified == [] and R._PENDING_REMINDER == {}
