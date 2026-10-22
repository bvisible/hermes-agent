# //// Neoffice — added file (no upstream equivalent): a conversation thread keeps its agent session (22.10).
"""A conversational webhook route keeps the agent's session for the thread the payload names.

The defect (osiris, 22.10): the `nora_chat` route keyed the agent's session on the person, and the
per-delivery close (``on_processing_complete`` -> ``end_session(..., "webhook_complete")``) ended it
after every run. The next message of the same thread found the row ended in state.db, the routing
self-heal minted a new session, and every turn began with ``history=0``: from an editor's Nora panel,
« J'applique le caviardage ? » then « Oui » reached an agent that had asked nothing.

These tests go through the REAL ``_spawn_agent_run`` -> ``handle_message`` ->
``_process_message_background`` -> ``on_processing_complete`` pipeline, with a real ``SessionStore``
and ``SessionDB``. Only the runner-side ``_message_handler`` is stubbed (the seam the live gateway
injects): it routes the event, loads the transcript as the runner does, records how much history the
turn saw, then appends the turn. The nora pre-router is stubbed to its answer for an editor turn
(DIRECT, with the editor's instruction), so no model is called.
"""

import asyncio
import time

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms import webhook as webhook_module
from gateway.platforms.webhook import WebhookAdapter, _INSECURE_NO_AUTH
from gateway.session import SessionSource, SessionStore

PERSON = "person@example.com"
EDITOR_CONTEXT = {"surface": "editor", "app": "pdfcraft", "document": {"name": "form.pdf"}}


class _FakeRunner:
    """The runner surface the webhook close path reads (same wiring as the live gateway)."""

    def __init__(self, store: SessionStore):
        self.session_store = store
        self._session_db = store._db

    def _session_key_for_source(self, source: SessionSource) -> str:
        return self.session_store._generate_session_key(source)

    def _profile_name_for_source(self, source, adapter_profile=None):
        return None


def _make_store(tmp_path) -> SessionStore:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    config = GatewayConfig(platforms={Platform.WEBHOOK: PlatformConfig(enabled=True)})
    store = SessionStore(sessions_dir=sessions_dir, config=config)
    assert store._db is not None, "test requires a real SessionDB"
    return store


def _route(session_key: str = "user") -> dict:
    return {"secret": _INSECURE_NO_AUTH, "prompt": "{message}", "deliver": "log", "session_key": session_key}


@pytest.fixture
def harness(tmp_path, monkeypatch):
    """An adapter on a real store whose handler records every turn's session and history."""
    from gateway import nora_chat_router

    monkeypatch.setattr(
        nora_chat_router, "route_chat_message",
        lambda **kwargs: {"routed": False, "category": "DIRECT", "ack": None, "task_id": None,
                          "agent_hint": "[editor instruction]"},
    )
    store = _make_store(tmp_path)
    adapter = WebhookAdapter(PlatformConfig(enabled=True, extra={"host": "127.0.0.1", "port": 0, "routes": {}}))
    adapter.gateway_runner = _FakeRunner(store)
    turns = []

    async def _message_handler(event):
        entry = store.get_or_create_session(event.source)
        history = store.load_transcript(entry.session_id)
        turns.append({"session_id": entry.session_id, "history": history, "chat_id": event.source.chat_id})
        store.append_to_transcript(entry.session_id, {"role": "user", "content": event.text})
        store.append_to_transcript(entry.session_id, {"role": "assistant", "content": f"answer to {event.text!r}"})
        return ""  # deliver=log: nothing to send back

    adapter._message_handler = _message_handler
    yield adapter, store, turns
    store._db.close()


async def _send(adapter, message: str, *, conversation_id=None, route_name="nora_chat", session_key="user",
                user=PERSON, context=EDITOR_CONTEXT):
    """One inbound POST, as _handle_webhook dispatches it, then wait for the run to finish."""
    payload = {"event_type": "chat", "user": user, "message": message, "context": context}
    if conversation_id is not None:
        payload["conversation_id"] = conversation_id
    delivery_id = f"{conversation_id}:{time.monotonic_ns()}"
    adapter._spawn_agent_run(payload, message, delivery_id, time.time(), route_config=_route(session_key),
                             route_name=route_name, profile=None, event_type="chat")
    deadline = asyncio.get_event_loop().time() + 5.0
    while adapter._background_tasks and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.05)  # the done-callbacks


def _asked(history) -> list:
    """The person's messages in a transcript, without the instruction the pre-router puts before each."""
    return [m["content"].rpartition("\n\n")[2] for m in history if m.get("role") == "user"]


@pytest.mark.asyncio
async def test_second_editor_turn_of_a_thread_sees_the_first(harness):
    """The reported defect: « Oui » in the same thread sees the question it answers."""
    adapter, store, turns = harness
    await _send(adapter, "Caviarde l'adresse e-mail puis applique.", conversation_id="nora-6599cd3abdf5410a")
    await _send(adapter, "Oui", conversation_id="nora-6599cd3abdf5410a")

    first, second = turns
    assert first["history"] == []
    assert second["session_id"] == first["session_id"], "the thread's second message got a new session"
    assert _asked(second["history"]) == ["Caviarde l'adresse e-mail puis applique."], (
        "the second turn does not see the first"
    )
    row = store._db.get_session(first["session_id"])
    assert row["ended_at"] is None and row["end_reason"] is None


@pytest.mark.asyncio
async def test_threads_of_one_person_stay_apart(harness):
    """Another document (another thread) or « Nouvelle conversation » starts from nothing."""
    adapter, store, turns = harness
    await _send(adapter, "Mets ces ventes dans un tableau.", conversation_id="nora-7abd296eccea4e56")
    await _send(adapter, "Remplis ce formulaire.", conversation_id="nora-6599cd3abdf5410a")
    await _send(adapter, "Ajoute mai et juin.", conversation_id="nora-7abd296eccea4e56")

    grid, pdf, grid_again = turns
    assert pdf["session_id"] != grid["session_id"]
    assert pdf["history"] == [], "a thread saw another thread's turns"
    assert grid_again["session_id"] == grid["session_id"]
    assert _asked(grid_again["history"]) == ["Mets ces ventes dans un tableau."]


@pytest.mark.asyncio
async def test_thread_chat_id_keeps_the_person_last(harness):
    """Two readers take the person from the end of the chat id: nora's callback (`user`) and the
    bridge user of a worker (tools/mcp_tool_handlers.py, after `:user:`)."""
    adapter, _store, turns = harness
    await _send(adapter, "Bonjour", conversation_id="nora-0929bee8086342b8")
    chat_id = turns[0]["chat_id"]
    assert chat_id == f"webhook:nora_chat:thread:nora-0929bee8086342b8:user:{PERSON}"
    assert chat_id.rsplit(":", 1)[-1] == PERSON
    assert chat_id.split(":user:", 1)[1] == PERSON
    assert chat_id.split(":")[1] == "nora_chat"  # send() rebuilds an expired delivery from the route


@pytest.mark.asyncio
async def test_payload_without_a_thread_is_still_closed_after_its_run(harness):
    """A conversational payload that names no thread (WhatsApp) keeps upstream's close."""
    adapter, store, turns = harness
    await _send(adapter, "Bonjour", conversation_id=None, route_name="person_chat")
    row = store._db.get_session(turns[0]["session_id"])
    assert turns[0]["chat_id"] == f"webhook:person_chat:user:{PERSON}"
    assert row["ended_at"] is not None and row["end_reason"] == "webhook_complete"


@pytest.mark.asyncio
async def test_a_thread_without_a_person_is_still_closed_after_its_run(harness):
    """No person in the payload: the delivery id stands in, one session per delivery, closed as before
    (a thread there would leave one open row per delivery and bring nothing back)."""
    adapter, store, turns = harness
    await _send(adapter, "Bonjour", conversation_id="nora-bbbbbbbbbbbbbbbb", route_name="person_chat", user="")
    row = store._db.get_session(turns[0]["session_id"])
    assert ":thread:" not in turns[0]["chat_id"]
    assert row["end_reason"] == "webhook_complete"


@pytest.mark.asyncio
async def test_per_delivery_route_is_still_closed_even_with_a_conversation_id(harness):
    """A `unique` route keeps one closed session per delivery, whatever the payload carries."""
    adapter, store, turns = harness
    await _send(adapter, "Alert", conversation_id="nora-aaaaaaaaaaaaaaaa", route_name="alerts", session_key="unique")
    row = store._db.get_session(turns[0]["session_id"])
    assert turns[0]["chat_id"].startswith("webhook:v2:")
    assert row["end_reason"] == "webhook_complete"


@pytest.mark.asyncio
async def test_two_people_naming_the_same_thread_never_share_it(harness):
    """A person who sends another's conversation_id gets neither that session, its history, nor its delivery
    (nora refuses another's thread first; the gateway must not depend on it)."""
    adapter, _store, turns = harness
    await _send(adapter, "Le code d'accès est 4711.", conversation_id="nora-1111111111111111", user="b@example.com")
    await _send(adapter, "Que disait le message précédent ?", conversation_id="nora-1111111111111111",
                user="a@example.com")

    b, a = turns
    assert a["session_id"] != b["session_id"]
    assert a["history"] == []
    assert a["chat_id"] != b["chat_id"]
    # The reply goes where the chat id says: nora's callback takes the person from its end.
    assert a["chat_id"].rsplit(":", 1)[-1] == "a@example.com"
    assert b["chat_id"].rsplit(":", 1)[-1] == "b@example.com"
    assert a["chat_id"] in adapter._delivery_info and b["chat_id"] in adapter._delivery_info


@pytest.mark.asyncio
async def test_a_monitoring_probe_gets_no_thread(harness):
    """Each probe sends a new conversation id and never a second message: no thread, closed after its run."""
    adapter, store, turns = harness
    await _send(adapter, "Bonjour !", conversation_id="nora-cccccccccccccccc", user="Administrator",
                context={"channel": "monitor"})
    row = store._db.get_session(turns[0]["session_id"])
    assert turns[0]["chat_id"] == "webhook:nora_chat:user:Administrator"
    assert row["end_reason"] == "webhook_complete"


@pytest.mark.asyncio
async def test_a_thread_idle_for_a_day_is_closed_and_starts_anew(harness):
    """prune_sessions reaps closed sessions only: a thread nobody wrote in for a day is closed by the next
    thread turn's sweep, and coming back to it starts a new session."""
    adapter, store, turns = harness
    await _send(adapter, "Mets ces ventes dans un tableau.", conversation_id="nora-7abd296eccea4e56")
    old = turns[0]["session_id"]
    day_ago = time.time() - 25 * 3600
    store._db._execute_write(lambda conn: (
        conn.execute("UPDATE sessions SET started_at = ?, last_activity_at = ? WHERE id = ?", (day_ago, day_ago, old)),
        conn.execute("UPDATE messages SET timestamp = ? WHERE session_id = ?", (day_ago, old)),
    ))
    adapter._neoffice_last_thread_sweep = 0.0  # the sweep runs at most every ten minutes

    await _send(adapter, "Remplis ce formulaire.", conversation_id="nora-6599cd3abdf5410a")
    assert store._db.get_session(old)["end_reason"] == "webhook_thread_idle"
    assert store._db.get_session(turns[1]["session_id"])["ended_at"] is None, "the live thread was closed"

    await _send(adapter, "Ajoute mai et juin.", conversation_id="nora-7abd296eccea4e56")
    assert turns[2]["session_id"] != old
    assert turns[2]["history"] == []


def _age(store, session_id, hours=25):
    """Make a session look untouched for *hours*: its heartbeat and its messages."""
    then = time.time() - hours * 3600
    store._db._execute_write(lambda conn: (
        conn.execute("UPDATE sessions SET started_at = ?, last_activity_at = ? WHERE id = ?", (then, then, session_id)),
        conn.execute("UPDATE messages SET timestamp = ? WHERE session_id = ?", (then, session_id)),
    ))


@pytest.mark.asyncio
async def test_an_idle_thread_whose_turn_has_started_keeps_its_session(harness):
    """A message arriving on a thread idle for a day holds the session's key while its turn runs: the
    sweep leaves that session alone."""
    adapter, store, turns = harness
    await _send(adapter, "Mets ces ventes dans un tableau.", conversation_id="nora-7abd296eccea4e56")
    session_id = turns[0]["session_id"]
    _age(store, session_id)
    key = store._db.get_session(session_id)["session_key"]
    adapter._active_sessions[key] = asyncio.Event()  # what handle_message holds while the turn runs
    adapter._neoffice_last_thread_sweep = 0.0
    assert await adapter._neoffice_close_idle_threads() == 0
    assert store._db.get_session(session_id)["ended_at"] is None

    del adapter._active_sessions[key]
    adapter._neoffice_last_thread_sweep = 0.0
    assert await adapter._neoffice_close_idle_threads() == 1
    assert store._db.get_session(session_id)["end_reason"] == "webhook_thread_idle"


@pytest.mark.asyncio
async def test_the_close_rechecks_the_idle_time_under_the_write_lock(harness):
    """A message written between the sweep's SELECT and its UPDATE keeps the session: the UPDATE
    tests the idle time again."""
    from contextlib import contextmanager
    from types import SimpleNamespace

    adapter, store, turns = harness
    await _send(adapter, "Bonjour", conversation_id="nora-dddddddddddddddd")
    session_id = turns[0]["session_id"]
    row = store._db.get_session(session_id)

    class _StaleSelect:
        """The real store, except that its SELECT still sees the session as idle."""

        def __init__(self, db):
            self._db = db

        @contextmanager
        def _read_ctx(self):
            yield SimpleNamespace(execute=lambda *a, **k: SimpleNamespace(
                fetchall=lambda: [(session_id, row["session_key"])]))

        def __getattr__(self, name):
            return getattr(self._db, name)

    closed = webhook_module._neoffice_end_idle_thread_sessions(_StaleSelect(store._db), time.time() - 86400, 50)
    assert closed == 0
    assert store._db.get_session(session_id)["ended_at"] is None


def test_a_thread_id_outside_the_safe_alphabet_becomes_a_digest():
    thread = webhook_module._neoffice_conversation_thread({"conversation_id": "a:b/c d"})
    assert thread.startswith("h") and len(thread) == 33 and ":" not in thread
    assert webhook_module._neoffice_conversation_thread({"conversation_id": "a:b/c d"}) == thread
    assert webhook_module._neoffice_conversation_thread({"conversation_id": "  "}) is None
    assert webhook_module._neoffice_conversation_thread({"conversation_id": 42}) is None
    assert webhook_module._neoffice_conversation_thread(None) is None
