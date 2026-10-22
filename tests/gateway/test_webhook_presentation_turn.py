# //// Neoffice — added file (no upstream equivalent): a presentation turn is marked by its own message (23.10).
"""The pre-router marks a presentation turn by the id of the message it routes (the webhook's delivery id), and the
run of that message carries the same id as its source's message id, which the gateway binds as
HERMES_SESSION_MESSAGE_ID: kanban_create (tools/kanban_tools.py) is refused in that turn only. The next message of the
same thread is another turn, never refused.

Through the REAL ``_spawn_agent_run`` -> ``handle_message`` pipeline, as test_webhook_conversation_thread_session.py:
the runner-side handler is stubbed to record what each turn's source says, and the pre-router to a presentation turn
for the first message and a plain DIRECT turn for the second, both marking as the real one does.
"""

import asyncio
import time

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.webhook import WebhookAdapter, _INSECURE_NO_AUTH
from gateway.session import SessionSource, SessionStore

PERSON = "person@example.com"
THREAD = "nora-0a1b2c3d4e5f6071"


class _FakeRunner:
    def __init__(self, store: SessionStore):
        self.session_store = store
        self._session_db = store._db

    def _session_key_for_source(self, source: SessionSource) -> str:
        return self.session_store._generate_session_key(source)

    def _profile_name_for_source(self, source, adapter_profile=None):
        return None


@pytest.fixture
def harness(tmp_path, monkeypatch):
    from gateway import nora_chat_router

    monkeypatch.setattr(nora_chat_router, "_DECK_TURNS", {})

    def route_chat_message(**kwargs):
        deck = nora_chat_router._is_deck_request(kwargs["message"])
        if deck:
            nora_chat_router._mark_deck_turn(kwargs["idempotency_key"])
        return {"routed": False, "category": "DIRECT", "ack": None, "task_id": None,
                "agent_hint": nora_chat_router._DECK_HINT if deck else None}

    monkeypatch.setattr(nora_chat_router, "route_chat_message", route_chat_message)
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    store = SessionStore(sessions_dir=sessions_dir,
                         config=GatewayConfig(platforms={Platform.WEBHOOK: PlatformConfig(enabled=True)}))
    adapter = WebhookAdapter(PlatformConfig(enabled=True, extra={"host": "127.0.0.1", "port": 0, "routes": {}}))
    adapter.gateway_runner = _FakeRunner(store)
    turns = []

    async def _message_handler(event):
        message_id = event.source.message_id
        turns.append({"message_id": message_id, "event_id": event.message_id,
                      "deck": nora_chat_router.deck_turn_under_way(message_id)})
        return ""

    adapter._message_handler = _message_handler
    yield adapter, turns
    store._db.close()


async def _send(adapter, message: str) -> str:
    payload = {"event_type": "chat", "user": PERSON, "message": message, "conversation_id": THREAD}
    delivery_id = f"{THREAD}:{time.monotonic_ns()}"
    adapter._spawn_agent_run(payload, message, delivery_id, time.time(),
                             route_config={"secret": _INSECURE_NO_AUTH, "prompt": "{message}", "deliver": "log",
                                           "session_key": "user"},
                             route_name="nora_chat", profile=None, event_type="chat")
    deadline = asyncio.get_event_loop().time() + 5.0
    while adapter._background_tasks and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.05)
    return delivery_id


@pytest.mark.asyncio
async def test_the_presentation_turn_and_the_next_one_are_told_apart(harness):
    adapter, turns = harness
    first = await _send(adapter, "Fais-moi une présentation de 3 diapositives sur nos horaires")
    second = await _send(adapter, "Combien de devis sont ouverts ?")
    assert [t["message_id"] for t in turns] == [first, second], "the run's source carries its delivery id"
    assert [t["event_id"] for t in turns] == [first, second]
    assert [t["deck"] for t in turns] == [True, False]
