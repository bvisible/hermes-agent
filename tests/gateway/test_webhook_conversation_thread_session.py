# //// Neoffice — added file (no upstream equivalent): a conversation thread keeps its agent session (22.10).
"""A conversational webhook route keeps the agent's session for the thread the payload names.

The defect (osiris, 22.10): the `nora_chat` route keyed the agent's session on the person, and the
per-delivery close (``on_processing_complete`` -> ``end_session(..., "webhook_complete")``) ended it
after every run. The next message of the same thread found the row ended in state.db, the routing
self-heal minted a new session, and every turn began with ``history=0``: from an editor's Nora panel,
« J'applique le caviardage ? » then « Oui » reached an agent that had asked nothing.

Since the durable queue (v0.22.3) a webhook message is committed by ``_admit_agent_run`` into a REAL
``SessionAuthority``'s admission FIFO, executed by its drain, then finalized (``finalize_webhook``, which
ends the session). These tests go through that real pipeline: our adapter code, the real admission and
its target session, the real settlement and finalization. Only the turn itself is stubbed
(``execute_finite_admission``, the seam upstream's own admission tests stub): it restores the committed
event as the drain does, loads the transcript of the target session, records how much history the turn
saw, then appends the turn. The nora pre-router is stubbed to its answer for an editor turn (DIRECT,
with the editor's instruction), so no model is called.
"""

import asyncio
import time
from types import SimpleNamespace

import pytest
import pytest_asyncio

from gateway.platforms import webhook as webhook_module
from gateway.platforms.webhook import _INSECURE_NO_AUTH
from tests.gateway.fixtures import neoffice_webhook_authority as authority_fixture
from tests.waiting import async_wait_until

PERSON = "person@example.com"
EDITOR_CONTEXT = {"surface": "editor", "app": "pdfcraft", "document": {"name": "form.pdf"}}


def _route(session_key: str = "user") -> dict:
    return {"secret": _INSECURE_NO_AUTH, "prompt": "{message}", "deliver": "log", "session_key": session_key}


def _whatsapp_route() -> dict:
    """The whatsapp_inbox route as provision writes it: keyed on the phone, the reply sent to {phone}."""
    return {**_route("phone"), "deliver_extra": {"phone": "{phone}", "router_url": "http://router.invalid"}}


def _desk_route() -> dict:
    """The nora_chat route as provision writes it: the desk callback, its token, the conversation id."""
    return {**_route("user"), "deliver": "nora", "deliver_extra": {
        "callback_url": "http://desk.invalid/api/method/nora.api.chat.deliver",
        "callback_token": "desk-token", "conversation_id": "{conversation_id}"}}


ROUTES = {"nora_chat": _route("user"), "person_chat": _route("user"), "alerts": _route("unique"),
          "whatsapp_inbox": _whatsapp_route(), "desk_chat": _desk_route()}


_all_rows = authority_fixture.all_rows
_settled = authority_fixture.settled
_admit = authority_fixture.admit


@pytest_asyncio.fixture
async def harness(tmp_path, monkeypatch):
    """An adapter on a real authority whose (stubbed) turn records every turn's session and history."""
    from gateway import nora_chat_router
    from gateway.neoffice_turn_memory import is_monitoring_event

    monkeypatch.setattr(
        nora_chat_router, "route_chat_message",
        lambda **kwargs: {"routed": False, "category": "DIRECT", "ack": None, "task_id": None,
                          "agent_hint": "[editor instruction]"},
    )
    turns = []
    hold = SimpleNamespace(gate=None)
    holder = {}

    async def turn(event, ref, history):
        store = holder["h"].store
        turns.append({"session_id": ref.session_id, "history": history, "chat_id": event.source.chat_id,
                      "chat_name": event.source.chat_name, "user_id": event.source.user_id,
                      "monitor": is_monitoring_event(event), "text": event.text})
        if hold.gate is not None:
            await hold.gate.wait()
        store.append_to_transcript(ref.session_id, {"role": "user", "content": event.text})
        store.append_to_transcript(ref.session_id, {"role": "assistant", "content": f"answer to {event.text!r}"})
        return ""  # nothing to deliver

    h = await authority_fixture.start(monkeypatch, ROUTES, turn)
    holder["h"] = h
    h.hold = hold
    yield h.adapter, h.store, turns, h
    await authority_fixture.stop(h)


async def _send(adapter, message: str, *, conversation_id=None, route_name="nora_chat", session_key="user",
                user=PERSON, context=EDITOR_CONTEXT):
    """One inbound POST, as _handle_webhook dispatches it, then wait for its turn and its finalizer."""
    payload = {"event_type": "chat", "user": user, "message": message, "context": context}
    if conversation_id is not None:
        payload["conversation_id"] = conversation_id
    delivery_id = f"{conversation_id}:{time.monotonic_ns()}"
    route = ROUTES.get(route_name, _route(session_key)) if session_key == "user" else _route(session_key)
    await _admit(adapter, payload, message, delivery_id, route_name, route)
    await _settled(adapter)


def _asked(history) -> list:
    """The person's messages in a transcript, without the instruction the pre-router puts before each."""
    return [m["content"].rpartition("\n\n")[2] for m in history if m.get("role") == "user"]


@pytest.mark.asyncio
async def test_second_editor_turn_of_a_thread_sees_the_first(harness):
    """The reported defect: « Oui » in the same thread sees the question it answers."""
    adapter, store, turns, _h = harness
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
    adapter, store, turns, _h = harness
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
    adapter, _store, turns, _h = harness
    await _send(adapter, "Bonjour", conversation_id="nora-0929bee8086342b8")
    chat_id = turns[0]["chat_id"]
    assert chat_id == f"webhook:nora_chat:thread:nora-0929bee8086342b8:user:{PERSON}"
    assert chat_id.rsplit(":", 1)[-1] == PERSON
    assert chat_id.split(":user:", 1)[1] == PERSON
    assert chat_id.split(":")[1] == "nora_chat"  # send() rebuilds an expired delivery from the route


@pytest.mark.asyncio
async def test_payload_without_a_thread_is_still_closed_after_its_run(harness):
    """A payload that names no thread on a route keyed on the person (not on a phone) keeps upstream's close."""
    adapter, store, turns, _h = harness
    await _send(adapter, "Bonjour", conversation_id=None, route_name="person_chat")
    row = store._db.get_session(turns[0]["session_id"])
    assert turns[0]["chat_id"] == f"webhook:person_chat:user:{PERSON}"
    assert row["ended_at"] is not None and row["end_reason"] == "webhook_complete"


@pytest.mark.asyncio
async def test_a_thread_without_a_person_is_still_closed_after_its_run(harness):
    """No person in the payload: the delivery id stands in, one session per delivery, closed as before
    (a thread there would leave one open row per delivery and bring nothing back)."""
    adapter, store, turns, _h = harness
    await _send(adapter, "Bonjour", conversation_id="nora-bbbbbbbbbbbbbbbb", route_name="person_chat", user="")
    row = store._db.get_session(turns[0]["session_id"])
    assert ":thread:" not in turns[0]["chat_id"]
    assert row["end_reason"] == "webhook_complete"


@pytest.mark.asyncio
async def test_per_delivery_route_is_still_closed_even_with_a_conversation_id(harness):
    """A `unique` route keeps one closed session per delivery, whatever the payload carries."""
    adapter, store, turns, _h = harness
    await _send(adapter, "Alert", conversation_id="nora-aaaaaaaaaaaaaaaa", route_name="alerts", session_key="unique")
    row = store._db.get_session(turns[0]["session_id"])
    assert turns[0]["chat_id"].startswith("webhook:v2:")
    assert row["end_reason"] == "webhook_complete"


@pytest.mark.asyncio
async def test_two_people_naming_the_same_thread_never_share_it(harness):
    """A person who sends another's conversation_id gets neither that session, its history, nor its delivery
    (nora refuses another's thread first; the gateway must not depend on it)."""
    adapter, _store, turns, _h = harness
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
    adapter, store, turns, _h = harness
    await _send(adapter, "Bonjour !", conversation_id="nora-cccccccccccccccc", user="Administrator",
                context={"channel": "monitor"})
    row = store._db.get_session(turns[0]["session_id"])
    assert turns[0]["chat_id"] == "webhook:nora_chat:user:Administrator"
    assert row["end_reason"] == "webhook_complete"


@pytest.mark.asyncio
async def test_a_thread_idle_for_a_day_is_closed_and_starts_anew(harness):
    """prune_sessions reaps closed sessions only: a thread nobody wrote in for a day is closed by the next
    thread turn's sweep, and coming back to it starts a new session."""
    adapter, store, turns, _h = harness
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
    """A message arriving on a thread idle for a day is admitted to that thread's session before its turn
    runs: while the admission is queued or running, the sweep leaves the session alone (the durable
    queue's own sign of a turn in flight, tested in the UPDATE)."""
    adapter, store, turns, h = harness
    await _send(adapter, "Mets ces ventes dans un tableau.", conversation_id="nora-7abd296eccea4e56")
    session_id = turns[0]["session_id"]
    _age(store, session_id)
    h.hold.gate = asyncio.Event()
    payload = {"event_type": "chat", "user": PERSON, "message": "Ajoute mai.", "context": EDITOR_CONTEXT,
               "conversation_id": "nora-7abd296eccea4e56"}
    await _admit(adapter, payload, "Ajoute mai.", f"held:{time.monotonic_ns()}", "nora_chat", _route("user"))
    await async_wait_until(lambda: len(turns) == 2, what="the held turn to start")
    assert turns[1]["session_id"] == session_id
    adapter._neoffice_last_thread_sweep = 0.0
    assert await adapter._neoffice_close_idle_threads(h.authority) == 0
    assert store._db.get_session(session_id)["ended_at"] is None

    h.hold.gate.set()
    h.hold.gate = None
    await _settled(adapter)
    _age(store, session_id)
    adapter._neoffice_last_thread_sweep = 0.0
    assert await adapter._neoffice_close_idle_threads(h.authority) == 1
    assert store._db.get_session(session_id)["end_reason"] == "webhook_thread_idle"


@pytest.mark.asyncio
async def test_the_close_rechecks_the_idle_time_under_the_write_lock(harness):
    """A message written between the sweep's SELECT and its UPDATE keeps the session: the UPDATE
    tests the idle time again."""
    from contextlib import contextmanager
    from types import SimpleNamespace

    adapter, store, turns, _h = harness
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


PHONE_A = "+41790000001"
PHONE_B = "+41790000002"


WA_USER = "linked.account@example.com"  # the instance account the router links to the number (#880)


async def _send_whatsapp(adapter, message: str, phone: str, **extra):
    """One WhatsApp message as the central router forwards it (forward-body.js): no conversation_id,
    the route keyed on the sender's phone, the linked account as `user`."""
    payload = {"phone": phone, "message": message, "timestamp": "2026-10-24T10:00:00Z", "user": WA_USER,
               "source": "whatsapp-router-v2", "message_id": f"wamid-{time.monotonic_ns()}", "is_audio": False}
    payload.update(extra)
    await _admit(adapter, payload, message, f"wa:{time.monotonic_ns()}", "whatsapp_inbox", _whatsapp_route())
    await _settled(adapter)


@pytest.mark.asyncio
async def test_a_whatsapp_chat_keeps_its_history_from_one_message_to_the_next(harness):
    """WhatsApp sends no conversation_id: the chat with the number is the thread (24.10)."""
    adapter, store, turns, _h = harness
    await _send_whatsapp(adapter, "Quelles factures sont échues ?", PHONE_A)
    await _send_whatsapp(adapter, "Et le mois dernier ?", PHONE_A)

    first, second = turns
    assert first["chat_id"] == f"webhook:whatsapp_inbox:thread:chat:phone:{PHONE_A}"
    assert second["session_id"] == first["session_id"]
    assert _asked(second["history"]) == ["Quelles factures sont échues ?"]
    assert store._db.get_session(first["session_id"])["ended_at"] is None


@pytest.mark.asyncio
async def test_two_whatsapp_numbers_never_share_a_thread(harness):
    """Even two numbers linked to the same account: the number keys the thread, and each reply goes to
    its own number."""
    adapter, _store, turns, _h = harness
    await _send_whatsapp(adapter, "Mon code d'accès est 4711.", PHONE_A)
    await _send_whatsapp(adapter, "Qu'a dit le message précédent ?", PHONE_B)

    a, b = turns
    assert b["session_id"] != a["session_id"]
    assert b["history"] == []
    assert b["chat_id"].endswith(f":phone:{PHONE_B}") and a["chat_id"].endswith(f":phone:{PHONE_A}")
    assert adapter._delivery_info[a["chat_id"]]["deliver_extra"]["phone"] == PHONE_A
    assert adapter._delivery_info[b["chat_id"]]["deliver_extra"]["phone"] == PHONE_B


@pytest.mark.parametrize("group", [
    {"is_group": True},
    {"group": True},
    {"remote_jid": "120363000000000000@g.us"},
    {"group_id": "120363000000000000@g.us"},
    {"from": "120363000000000000@G.US"},
    {"key": {"remoteJid": "120363000000000000@g.us"}},
])
@pytest.mark.asyncio
async def test_a_group_message_is_never_taken_for_the_persons_chat(harness, group):
    """The central router drops group messages today; if one came, it would get no thread, never the
    sender's own chat, and its session would close after its run."""
    adapter, store, turns, _h = harness
    await _send_whatsapp(adapter, "Bonjour, ici la personne.", PHONE_A)
    await _send_whatsapp(adapter, "Bonjour le groupe.", PHONE_A, **group)

    chat, in_group = turns
    assert ":thread:" not in in_group["chat_id"]
    assert in_group["session_id"] != chat["session_id"]
    assert in_group["history"] == []
    assert store._db.get_session(in_group["session_id"])["end_reason"] == "webhook_complete"
    assert store._db.get_session(chat["session_id"])["ended_at"] is None


@pytest.mark.asyncio
async def test_a_whatsapp_chat_idle_for_a_day_is_closed_and_starts_anew(harness):
    adapter, store, turns, _h = harness
    await _send_whatsapp(adapter, "Quelles factures sont échues ?", PHONE_A)
    old = turns[0]["session_id"]
    _age(store, old)
    adapter._neoffice_last_thread_sweep = 0.0
    await _send_whatsapp(adapter, "Bonjour", PHONE_B)  # any thread's turn runs the sweep
    assert store._db.get_session(old)["end_reason"] == "webhook_thread_idle"

    await _send_whatsapp(adapter, "Et aujourd'hui ?", PHONE_A)
    assert turns[2]["session_id"] != old
    assert turns[2]["history"] == []


@pytest.mark.asyncio
async def test_a_whatsapp_message_31_minutes_later_starts_without_history(harness):
    """The central router renews a WhatsApp thread after 30 minutes of silence: the agent's session ends with
    it, so a « oui » sent later never confirms an old question (24.10)."""
    adapter, store, turns, _h = harness
    await _send_whatsapp(adapter, "Supprime le rappel de vendredi.", PHONE_A)
    old = turns[0]["session_id"]
    _age(store, old, hours=31 / 60)

    await _send_whatsapp(adapter, "Oui", PHONE_A)
    assert turns[1]["session_id"] != old
    assert turns[1]["history"] == []
    assert store._db.get_session(old)["end_reason"] == "webhook_thread_idle"


@pytest.mark.asyncio
async def test_a_whatsapp_message_29_minutes_later_keeps_the_thread(harness):
    adapter, store, turns, _h = harness
    await _send_whatsapp(adapter, "Quelles factures sont échues ?", PHONE_A)
    _age(store, turns[0]["session_id"], hours=29 / 60)

    await _send_whatsapp(adapter, "Et le mois dernier ?", PHONE_A)
    assert turns[1]["session_id"] == turns[0]["session_id"]
    assert _asked(turns[1]["history"]) == ["Quelles factures sont échues ?"]


@pytest.mark.asyncio
async def test_a_desk_thread_31_minutes_later_keeps_its_day(harness):
    """Only WhatsApp follows the router's 30 minutes: a desk or editor thread keeps its 24 h."""
    adapter, store, turns, _h = harness
    await _send(adapter, "Caviarde l'adresse e-mail puis applique.", conversation_id="nora-6599cd3abdf5410a")
    _age(store, turns[0]["session_id"], hours=31 / 60)

    await _send(adapter, "Oui", conversation_id="nora-6599cd3abdf5410a")
    assert turns[1]["session_id"] == turns[0]["session_id"]
    assert _asked(turns[1]["history"]) == ["Caviarde l'adresse e-mail puis applique."]


@pytest.mark.parametrize("extra", [
    {},
    {"conversation_id": "41790000002"},  # a thread id made of digits, a number's look-alike
    {"conversation_id": "550e8400-e29b-41d4-a716-446655440000"},
])
@pytest.mark.asyncio
async def test_a_late_whatsapp_reply_still_finds_its_number(harness, monkeypatch, extra):
    """A pole's late answer comes back by the chat id alone once the delivery info expired: send() reads
    the number after the last `:phone:` of the chat id, whatever thread goes before it."""
    adapter, _store, turns, _h = harness
    await _send_whatsapp(adapter, "Quelles factures sont échues ?", PHONE_A, **extra)
    chat_id = turns[0]["chat_id"]
    adapter._delivery_info.pop(chat_id, None)
    adapter._routes = {"whatsapp_inbox": {"deliver": "whatsapp_router",
                                          "deliver_extra": {"phone": "{phone}", "router_url": "http://router.invalid"}}}
    sent = {}

    async def _capture(content, delivery):
        sent.update(delivery)
        from gateway.platforms.base import SendResult

        return SendResult(success=True)

    monkeypatch.setattr(adapter, "_deliver_whatsapp_router", _capture)
    await adapter.send(chat_id, "Voici les factures.", metadata={"conversation_id": "late-1"})
    assert sent["deliver_extra"]["phone"] == PHONE_A


def test_a_thread_id_outside_the_safe_alphabet_becomes_a_digest():
    thread = webhook_module._neoffice_conversation_thread({"conversation_id": "a:b/c d"})
    assert thread.startswith("h") and len(thread) == 33 and ":" not in thread
    assert webhook_module._neoffice_conversation_thread({"conversation_id": "a:b/c d"}) == thread
    assert webhook_module._neoffice_conversation_thread({"conversation_id": "  "}) is None
    assert webhook_module._neoffice_conversation_thread({"conversation_id": 42}) is None
    assert webhook_module._neoffice_conversation_thread(None) is None


# The durable queue (v0.22.3): what it changed for a conversation, end to end through the real authority.


@pytest.mark.asyncio
async def test_the_startup_recovery_keeps_a_thread_open(harness):
    """Upstream closes every settled webhook session again at start (recover_webhook_finalizations): a thread's
    session is kept, a one-shot person session is closed as before."""
    from gateway.platforms.webhook_ingress import recover_webhook_finalizations

    adapter, store, turns, h = harness
    await _send(adapter, "Caviarde l'adresse e-mail puis applique.", conversation_id="nora-6599cd3abdf5410a")
    await recover_webhook_finalizations(h.authority)
    assert store._db.get_session(turns[0]["session_id"])["ended_at"] is None

    await _send(adapter, "Oui", conversation_id="nora-6599cd3abdf5410a")
    assert turns[1]["session_id"] == turns[0]["session_id"]
    assert _asked(turns[1]["history"]) == ["Caviarde l'adresse e-mail puis applique."]


@pytest.mark.asyncio
async def test_a_message_the_router_routes_is_never_admitted(harness, monkeypatch):
    """A message the pre-router consumed (task created, ack posted) is never committed: no admission, no turn."""
    from gateway import nora_chat_router

    adapter, _store, turns, h = harness
    seen = {}

    def routed(**kwargs):
        seen.update(kwargs)
        return {"routed": True, "category": "compta", "ack": "…", "task_id": "t_fixture"}

    monkeypatch.setattr(nora_chat_router, "route_chat_message", routed)
    payload = {"event_type": "chat", "user": PERSON, "message": "Combien de factures impayées ?",
               "conversation_id": "nora-0a1b2c3d4e5f6071", "language": "fr"}
    await _admit(adapter, payload, "Combien de factures impayées ?", "routed-1", "nora_chat", _route("user"))
    await asyncio.sleep(0.2)
    assert _all_rows(h.authority) == [] and turns == []
    assert seen["session_chat_id"] == f"webhook:nora_chat:thread:nora-0a1b2c3d4e5f6071:user:{PERSON}"
    assert seen["idempotency_key"] == "routed-1" and seen["language"] == "fr"


@pytest.mark.asyncio
async def test_a_monitoring_probe_is_named_on_the_committed_source(harness):
    """The committed event keeps no raw payload: the probe is recognised from its source when its turn runs."""
    adapter, _store, turns, _h = harness
    await _send(adapter, "Bonjour !", conversation_id="nora-cccccccccccccccc", user="Administrator",
                context={"channel": "monitor"})
    await _send(adapter, "Bonjour !", conversation_id="nora-dddddddddddddddd")
    assert turns[0]["chat_name"] == "webhook/nora_chat/monitor" and turns[0]["monitor"] is True
    assert turns[1]["chat_name"] == "webhook/nora_chat" and turns[1]["monitor"] is False


@pytest.mark.asyncio
async def test_a_reply_without_the_memory_finds_the_conversation_destination(harness, monkeypatch):
    """A conversation's chat id is shared by all its admissions (upstream's retained lookup wanted exactly one):
    with the in-memory info gone (a restart), a reply goes to the newest admission's destination, with the
    route's callback and token put back (they are never written into the queue)."""
    from gateway.platforms.base import SendResult

    adapter, _store, turns, h = harness
    await _send(adapter, "Premier message.", conversation_id="nora-0a1b2c3d4e5f6071", route_name="desk_chat")
    await _send(adapter, "Second message.", conversation_id="nora-0a1b2c3d4e5f6071", route_name="desk_chat")
    chat_id = turns[1]["chat_id"]
    assert chat_id == turns[0]["chat_id"] == f"webhook:desk_chat:thread:nora-0a1b2c3d4e5f6071:user:{PERSON}"
    rows = _all_rows(h.authority)
    assert len(rows) == 2
    for row in rows:
        retained = row["payload"]["native_text_v1"]["webhook_delivery"]
        assert retained == {"deliver": "nora", "deliver_extra": {"conversation_id": "nora-0a1b2c3d4e5f6071"}}
        assert "desk-token" not in str(row["payload"])

    sent = []

    async def capture(content, chat_id_, delivery):
        sent.append((content, chat_id_, delivery))
        return SendResult(success=True)

    monkeypatch.setattr(adapter, "_deliver_nora", capture)
    adapter._delivery_info.clear()
    result = await adapter.send(chat_id, "Réponse tardive.")
    assert result.success, result.error
    content, sent_chat, delivery = sent[0]
    assert sent_chat == chat_id and content == "Réponse tardive."
    assert delivery["deliver_extra"] == {"callback_url": "http://desk.invalid/api/method/nora.api.chat.deliver",
                                         "callback_token": "desk-token",
                                         "conversation_id": "nora-0a1b2c3d4e5f6071"}


@pytest.mark.asyncio
async def test_a_status_never_reaches_a_customer_chat_without_the_memory(harness, monkeypatch):
    """Interim statuses are kept out of a customer chat by its ROUTE: once the in-memory info is gone, send()
    would still find the chat's destination (above), so the route decides, not the info."""
    adapter, _store, turns, _h = harness
    await _send(adapter, "Premier message.", conversation_id="nora-0a1b2c3d4e5f6071", route_name="desk_chat")
    delivered = []

    async def capture(content, chat_id_, delivery):
        delivered.append(content)

    monkeypatch.setattr(adapter, "_deliver_nora", capture)
    adapter._delivery_info.clear()
    result = await adapter.send_or_update_status(turns[0]["chat_id"], "retry", "⏳ Retrying in 2.9s (attempt 1/3)...")
    assert result.success and delivered == []
