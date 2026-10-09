# //// Neoffice — added file (no upstream equivalent): a WhatsApp chat has a thread (09.10).
"""A follow-up on WhatsApp keeps its context, as it does in a desk thread.

The desk sends a conversation_id per thread; a WhatsApp payload has none, and the router's whole context
(the prior pole, the film given to a worker, a pending question) keyed on it: « Oui, vas-y » after a
question to the accounting pole was routed as a first message, and the worker of a follow-up never saw
what came before. The chat (the number) is now the thread, renewed after half an hour of silence.
"""
import sys
import types
from types import SimpleNamespace

import pytest

from gateway import nora_chat_router as R

_PHONE = "+41790000000"
_CHAT = f"webhook:whatsapp_inbox:phone:{_PHONE}"
_WHATSAPP = {"router_url": "https://hub.example.test/whatsapp-router", "router_api_key": "router-key",
             "phone": _PHONE, "is_audio": "false"}
_STATE = (R._LAST_ROUTE, R._PENDING_SPACE, R._PENDING_NOTE, R._CONV_HISTORY, R._SPACE_ASKED,
          R._PENDING_REMINDER, getattr(R, "_CHAT_THREADS", {}))


class _Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now


@pytest.fixture
def chat(monkeypatch):
    import urllib.request

    for d in _STATE:
        d.clear()
    clock = _Clock()
    monkeypatch.setattr(R, "_thread_now", clock, raising=False)
    created, subs, sent = [], [], []

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return b"{}"

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: sent.append(req.full_url) or _Resp())
    monkeypatch.setattr(R, "_asks_fast_answer", lambda *a, **k: False)

    def create_task(conn, **kw):
        created.append(kw)
        return f"t_{len(created)}"

    kanban_db = types.SimpleNamespace(create_task=create_task)
    connect = types.SimpleNamespace(connect=lambda board=None: SimpleNamespace(close=lambda: None))
    notify = types.SimpleNamespace(add_notify_sub=lambda conn, **kw: subs.append(kw))
    pkg = sys.modules.get("hermes_cli") or types.ModuleType("hermes_cli")
    monkeypatch.setitem(sys.modules, "hermes_cli", pkg)
    for name, mod in (("kanban_db", kanban_db), ("kanban_db_connect", connect), ("kanban_db_notify", notify)):
        monkeypatch.setattr(pkg, name, mod, raising=False)
        monkeypatch.setitem(sys.modules, f"hermes_cli.{name}", mod)

    def llm(**_kw):  # the model reads « direct » whenever it is asked
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="direct"))])

    def route(message, conversation_id=None):
        return R.route_chat_message(
            message=message, session_chat_id=_CHAT, conversation_id=conversation_id, thread_id=None,
            user_id=_PHONE, notifier_profile="default", idempotency_key=f"k{len(created)}-{clock.now}",
            call_llm_fn=llm, main_runtime=None, deliver_extra=dict(_WHATSAPP), chat_user="staff@example.test",
            language="fr", chat_phone=_PHONE,
        )

    yield SimpleNamespace(route=route, clock=clock, created=created, subs=subs, sent=sent)
    R.flush_whatsapp_sends()  # the acks posted in the background, before urlopen is restored
    for d in _STATE:
        d.clear()


def test_a_go_ahead_on_whatsapp_stays_with_the_pole_it_answers(chat):
    first = chat.route("Combien de factures impayées avons-nous ?")
    assert first["category"] == "compta", first
    cid = chat.subs[0]["delivery_metadata"]["conversation_id"]
    assert cid.startswith(_CHAT + "#"), chat.subs[0]
    # The notifier records the worker's answer under the thread it was subscribed with.
    R.note_nora_reply(cid, "Vous avez 12 factures impayées. Voulez-vous que je relance les clients ?")

    chat.clock.now += 600
    second = chat.route("Oui, vas-y")
    assert second["category"] == "compta", second
    body = chat.created[-1]["body"]
    assert "Combien de factures impayées" in body and "Voulez-vous que je relance" in body, body
    assert chat.subs[-1]["delivery_metadata"]["conversation_id"] == cid


def test_after_half_an_hour_of_silence_the_chat_starts_a_new_thread(chat):
    chat.route("Combien de factures impayées avons-nous ?")
    first_cid = chat.subs[0]["delivery_metadata"]["conversation_id"]

    chat.clock.now += R._CHAT_THREAD_IDLE + 1
    later = chat.route("Oui, vas-y")
    assert later["category"] != "compta", later
    assert R._CHAT_THREADS[_CHAT][0] != first_cid


def test_noras_reply_keeps_the_thread_alive():
    R._CHAT_THREADS.clear()
    R._CONV_HISTORY.clear()
    now = [1_800_000_000.0]
    real = R._thread_now
    R._thread_now = lambda: now[0]
    try:
        key = R.chat_thread(_CHAT)
        now[0] += 1500
        R.note_nora_reply(key, "Voici la liste.")
        now[0] += 1500                      # 50 min after the person's message, 25 after NORA's reply
        assert R.chat_thread(_CHAT) == key
    finally:
        R._thread_now = real
        R._CHAT_THREADS.clear()
        R._CONV_HISTORY.clear()


def test_a_desk_thread_keeps_its_own_conversation_id(chat):
    chat.route("Combien de factures impayées avons-nous ?", conversation_id="conv-desk")
    assert chat.subs[0]["delivery_metadata"]["conversation_id"] == "conv-desk"
    assert R._CHAT_THREADS == {}
