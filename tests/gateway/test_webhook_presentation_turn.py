# //// Neoffice — added file (no upstream equivalent): a presentation turn is marked by its own message (23.10).
"""The pre-router marks a presentation turn by the id of the message it routes (the webhook's delivery id), and the
run of that message carries the same id as its source's message id, which the gateway binds as
HERMES_SESSION_MESSAGE_ID: kanban_create (tools/kanban_tools.py) is refused in that turn only. The next message of the
same thread is another turn, never refused.

Through the real durable pipeline (tests/gateway/fixtures/neoffice_webhook_authority.py): admission by
``_admit_agent_run``, the authority's FIFO, the event restored from what was committed. Since v0.22.3 the run reads
the COMMITTED source, so the delivery id must survive the queue. The turn is stubbed to record what each turn's
source says, and the pre-router to a presentation turn for the first message and a plain DIRECT turn for the
second, both marking as the real one does.
"""

import time

import pytest
import pytest_asyncio

from gateway.platforms.webhook import _INSECURE_NO_AUTH
from tests.gateway.fixtures import neoffice_webhook_authority as authority_fixture

PERSON = "person@example.com"
THREAD = "nora-0a1b2c3d4e5f6071"
ROUTE = {"secret": _INSECURE_NO_AUTH, "prompt": "{message}", "deliver": "log", "session_key": "user"}


@pytest_asyncio.fixture
async def harness(tmp_path, monkeypatch):
    from gateway import nora_chat_router

    monkeypatch.setattr(nora_chat_router, "_DECK_TURNS", {})

    def route_chat_message(**kwargs):
        deck = nora_chat_router._is_deck_request(kwargs["message"])
        if deck:
            nora_chat_router._mark_deck_turn(kwargs["idempotency_key"])
        return {"routed": False, "category": "DIRECT", "ack": None, "task_id": None,
                "agent_hint": nora_chat_router._DECK_HINT if deck else None}

    monkeypatch.setattr(nora_chat_router, "route_chat_message", route_chat_message)
    turns = []

    async def turn(event, ref, history):
        message_id = event.source.message_id
        turns.append({"message_id": message_id, "event_id": event.message_id,
                      "deck": nora_chat_router.deck_turn_under_way(message_id)})

    h = await authority_fixture.start(monkeypatch, {"nora_chat": ROUTE}, turn)
    yield h.adapter, turns
    await authority_fixture.stop(h)


async def _send(adapter, message: str) -> str:
    payload = {"event_type": "chat", "user": PERSON, "message": message, "conversation_id": THREAD}
    delivery_id = f"{THREAD}:{time.monotonic_ns()}"
    await authority_fixture.admit(adapter, payload, message, delivery_id, "nora_chat", ROUTE)
    await authority_fixture.settled(adapter)
    return delivery_id


@pytest.mark.asyncio
async def test_the_presentation_turn_and_the_next_one_are_told_apart(harness):
    adapter, turns = harness
    first = await _send(adapter, "Fais-moi une présentation de 3 diapositives sur nos horaires")
    second = await _send(adapter, "Combien de devis sont ouverts ?")
    assert [t["message_id"] for t in turns] == [first, second], "the run's source carries its delivery id"
    assert [t["event_id"] for t in turns] == [first, second]
    assert [t["deck"] for t in turns] == [True, False]
