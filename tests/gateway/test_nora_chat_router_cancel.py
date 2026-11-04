# //// Neoffice — added file (no upstream equivalent): the person stops or replaces a request in flight (#1568).
"""« Non, laisse tomber » stops the pole's task; « non, plutôt dix pièces » replaces it.

nora names every message it sends by a request id, the webhook delivery id « <conversation>:<ms> » that is also the
router task's idempotency key. A stop names the request ids to stop: a task not over yet is archived (its worker is
terminated, and refuses any tool meanwhile), a request not routed yet never becomes a task, and every reply of a
request names it so that nora drops what a stopped request still sends.
"""
import asyncio
import io
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway import nora_chat_router as R

CID = "nora-0123456789abcdef"
RID = f"{CID}:1793791466639"
_CB = "https://erp.example.test/api/method/nora.api.v2.hermes_callback.deliver"
_EXTRA = {"callback_url": _CB, "callback_token": "tok", "conversation_id": CID}


@pytest.fixture(autouse=True)
def fresh_state():
    dicts = (R._LAST_ROUTE, R._PENDING_SPACE, R._PENDING_NOTE, R._CONV_HISTORY, R._SPACE_ASKED, R._CANCELLED_REQUESTS)
    for d in dicts:
        d.clear()
    R._CURRENT_REQUEST.set("")
    yield
    for d in dicts:
        d.clear()
    R._CURRENT_REQUEST.set("")


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A real kanban board in a temporary home."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_HOME", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kbc.connect()
    yield kb, conn
    conn.close()


def _task(kb, conn, key, status="running", block_kind=None):
    tid = kb.create_task(conn, title="t", assignee="ventes", idempotency_key=key, initial_status="running")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = ?, block_kind = ? WHERE id = ?", (status, block_kind, tid))
    return tid


def _blocked(kb, conn, key, kind, assignee="ventes"):
    """Blocked as a worker blocks it (claim, then kanban_block): the board keeps it blocked."""
    tid = kb.create_task(conn, title="t", assignee=assignee, idempotency_key=key)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
    assert kb.claim_task(conn, tid, claimer="worker") is not None
    assert kb.block_task(conn, tid, reason="combien d'unités ?", kind=kind)
    return tid


def _status(conn, tid):
    return conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()[0]


# ── the request id ───────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("key, expected", [
    (RID, RID),
    ("k", ""),                                        # a test's, or a provider's: no request key
    ("nora-other0000000000:1793791466639", ""),       # another conversation's
    (f"{CID}:abc", ""),
    (f"{CID}:1793791466639:x", ""),
])
def test_only_the_conversations_own_request_key_is_a_request_id(key, expected):
    assert R._request_id_of(CID, key) == expected


def test_nothing_is_a_request_without_a_conversation():
    assert R._request_id_of(None, RID) == ""


# ── stopping ─────────────────────────────────────────────────────────────────────────────────────────────────────

def test_a_task_at_work_is_archived(board):
    kb, conn = board
    tid = _task(kb, conn, RID)
    out = R.cancel_chat_requests(conversation_id=CID, request_ids=[RID])
    assert out[RID]["outcome"] == "stopped" and out[RID]["task_id"] == tid
    assert _status(conn, tid) == "archived"
    assert R.request_cancelled(RID)


@pytest.mark.parametrize("status, outcome", [("done", "already_done"), ("archived", "already_stopped")])
def test_a_task_over_is_said_as_it_is(board, status, outcome):
    kb, conn = board
    tid = _task(kb, conn, RID, status=status)
    out = R.cancel_chat_requests(conversation_id=CID, request_ids=[RID])
    assert out[RID]["outcome"] == outcome
    assert _status(conn, tid) == status


def test_a_task_blocked_on_a_question_is_stopped_too(board):
    kb, conn = board
    tid = _blocked(kb, conn, RID, "needs_input")
    assert R.cancel_chat_requests(conversation_id=CID, request_ids=[RID])[RID]["outcome"] == "stopped"
    assert _status(conn, tid) == "archived"


def test_another_conversations_request_is_refused_and_untouched(board):
    kb, conn = board
    other = "nora-ffffffffffffffff:1793791466639"
    tid = _task(kb, conn, other)
    out = R.cancel_chat_requests(conversation_id=CID, request_ids=[other])
    assert out[other]["outcome"] == "refused"
    assert _status(conn, tid) == "running"
    assert not R.request_cancelled(other)


def test_only_the_named_request_is_stopped(board):
    kb, conn = board
    later = f"{CID}:1793791499999"
    first, second = _task(kb, conn, RID), _task(kb, conn, later)
    R.cancel_chat_requests(conversation_id=CID, request_ids=[RID])
    assert _status(conn, first) == "archived" and _status(conn, second) == "running"


def test_a_request_not_routed_yet_is_remembered(board):
    out = R.cancel_chat_requests(conversation_id=CID, request_ids=[RID])
    assert out[RID] == {"outcome": "not_started", "task_id": None}
    assert R.request_cancelled(RID)


@pytest.mark.parametrize("status, words", [
    ("running", "before it was finished: do not carry it out"),
    ("done", "after it was done: what it did stays done"),
])
def test_the_stop_is_said_in_the_conversations_film(board, status, words):
    # The next worker reads the person's turns: the stopped one must not read as done (or as never done when it was).
    kb, conn = board
    _task(kb, conn, RID, status=status)
    R.cancel_chat_requests(conversation_id=CID, request_ids=[RID])
    assert words in R._CONV_HISTORY[CID][-1] and R._CONV_HISTORY[CID][-1].startswith("NORA: [The person stopped")


@pytest.mark.parametrize("status, words", [
    ("running", "Carry out « t » as the message below changes it, now, without asking again"),
    ("done", "Change what it made as the message asks; never make it a second time"),
])
def test_a_correction_is_said_as_the_request_changed_not_dropped(board, status, words):
    # Told only « do not carry it out », the worker of « non, plutôt dix pièces » asked again (osiris, 04.11).
    kb, conn = board
    _task(kb, conn, RID, status=status)
    R.cancel_chat_requests(conversation_id=CID, request_ids=[RID], reason="correction")
    assert words in R._CONV_HISTORY[CID][-1]


def test_a_request_stopped_before_its_task_is_said_never_done(board):
    R.cancel_chat_requests(conversation_id=CID, request_ids=[RID])
    assert "nothing of it was done" in R._CONV_HISTORY[CID][-1]


def test_a_stop_is_forgotten_after_an_hour(monkeypatch):
    R._CANCELLED_REQUESTS[RID] = (R._time_req.time() - R._CANCELLED_KEEP_S - 1, "cancel")
    assert not R.request_cancelled(RID)


# ── the router ───────────────────────────────────────────────────────────────────────────────────────────────────

class _Http:
    def __init__(self):
        self.calls = []

    def __call__(self, req, timeout=None):
        self.calls.append(json.loads(req.data.decode()))
        return _Resp()


class _Resp:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return b"{}"


def _fake_kanban(monkeypatch, on_create=None):
    made = {"created": [], "subs": [], "archived": []}

    def create_task(conn, **kw):
        made["created"].append(kw)
        if on_create:
            on_create()
        return "t_new"

    kanban_db = types.SimpleNamespace(create_task=create_task,
                                      archive_task=lambda conn, tid: made["archived"].append(tid) or True)
    connect = types.SimpleNamespace(connect=lambda board=None: SimpleNamespace(
        close=lambda: None, execute=lambda *a: SimpleNamespace(fetchall=list)))
    notify = types.SimpleNamespace(add_notify_sub=lambda conn, **kw: made["subs"].append(kw))
    pkg = sys.modules.get("hermes_cli") or types.ModuleType("hermes_cli")
    monkeypatch.setitem(sys.modules, "hermes_cli", pkg)
    for name, mod in (("kanban_db", kanban_db), ("kanban_db_connect", connect), ("kanban_db_notify", notify)):
        monkeypatch.setattr(pkg, name, mod, raising=False)
        monkeypatch.setitem(sys.modules, f"hermes_cli.{name}", mod)
    return made


def _route(monkeypatch, message="Crée un devis pour Daniel Moret avec cinq pièces.", key=RID, classify=None):
    import urllib.request

    http = _Http()
    monkeypatch.setattr(urllib.request, "urlopen", http)
    monkeypatch.setattr(R, "_asks_fast_answer", lambda *a, **k: False)
    monkeypatch.setattr(R, "classify", classify or (lambda message, **kw: "ventes"))
    decision = R.route_chat_message(
        message=message, session_chat_id="webhook:nora_chat:test", conversation_id=CID,
        thread_id=None, user_id="u", notifier_profile="default", idempotency_key=key,
        call_llm_fn=lambda **kw: None, main_runtime=None, deliver_extra=dict(_EXTRA),
        chat_user="staff@example.test", language="fr", page_context=None, nora_spoke=True)
    return decision, http


def test_a_routed_request_names_itself_in_its_ack_and_its_result(monkeypatch):
    made = _fake_kanban(monkeypatch)
    decision, http = _route(monkeypatch)
    assert decision["routed"] is True and decision["task_id"] == "t_new"
    assert made["created"][0]["idempotency_key"] == RID
    assert made["subs"][0]["delivery_metadata"]["neoffice_request_id"] == RID
    assert http.calls and all(call.get("request_id") == RID for call in http.calls)


def test_a_message_without_a_request_key_tags_nothing(monkeypatch):
    made = _fake_kanban(monkeypatch)
    _decision, http = _route(monkeypatch, key="k")
    assert "neoffice_request_id" not in made["subs"][0]["delivery_metadata"]
    assert http.calls and all("request_id" not in call for call in http.calls)


def test_a_request_stopped_before_it_arrives_is_never_routed(monkeypatch):
    made = _fake_kanban(monkeypatch)
    R._CANCELLED_REQUESTS[RID] = (R._time_req.time(), "cancel")
    decision, http = _route(monkeypatch)
    assert decision == {"routed": True, "category": "CANCELLED", "ack": None, "task_id": None}
    assert made["created"] == [] and http.calls == []


def test_a_request_stopped_while_it_is_classified_makes_no_task(monkeypatch):
    made = _fake_kanban(monkeypatch)

    def classify(message, **kw):
        R._CANCELLED_REQUESTS[RID] = (R._time_req.time(), "cancel")
        return "ventes"

    decision, http = _route(monkeypatch, classify=classify)
    assert decision["category"] == "CANCELLED" and made["created"] == [] and http.calls == []


def test_a_stop_that_crosses_the_creation_archives_the_task(monkeypatch):
    made = _fake_kanban(monkeypatch, on_create=lambda: R._CANCELLED_REQUESTS.update({RID: (R._time_req.time(), "c")}))
    decision, http = _route(monkeypatch)
    assert decision["category"] == "CANCELLED"
    assert made["archived"] == ["t_new"] and http.calls == []


# ── the questions left unanswered ────────────────────────────────────────────────────────────────────────────────

def test_the_conversations_questions_are_retired_when_a_new_task_is_made(board):
    kb, conn = board
    asked = _blocked(kb, conn, f"{CID}:1793791400000", "needs_input")
    waiting = _blocked(kb, conn, f"{CID}:1793791400001", "capability")
    elsewhere = _blocked(kb, conn, "nora-ffffffffffffffff:1793791400002", "needs_input")
    other_pole = _blocked(kb, conn, f"{CID}:1793791400003", "needs_input", assignee="compta")
    by_orchestrator = _blocked(kb, conn, None, "needs_input")
    new = _task(kb, conn, RID)
    assert R._retire_unanswered_questions(kb, conn, CID, new, "ventes") == [asked]
    assert [_status(conn, t) for t in (asked, waiting, elsewhere, other_pole, by_orchestrator, new)] == [
        "archived", "blocked", "blocked", "blocked", "blocked", "running"]


# ── the worker's safe point ──────────────────────────────────────────────────────────────────────────────────────

def test_a_worker_whose_task_was_stopped_calls_no_tool(board, monkeypatch):
    kb, conn = board
    from tools import mcp_tool_handlers as H

    tid = _task(kb, conn, RID)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    assert H._neoffice_task_stopped() is None
    kb.archive_task(conn, tid)
    assert H._neoffice_task_stopped() == H._NEOFFICE_STOPPED_TEXT


def test_the_safe_point_never_fails_a_call(monkeypatch, tmp_path):
    from tools import mcp_tool_handlers as H

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    assert H._neoffice_task_stopped() is None
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_x")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "missing" / "kanban.db"))
    assert H._neoffice_task_stopped() is None


# ── the webhook ──────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_webhook_stop_answers_what_happened(board, monkeypatch):
    from gateway.platforms import webhook as W

    kb, conn = board
    tid = _task(kb, conn, RID)
    adapter = W.WebhookAdapter.__new__(W.WebhookAdapter)
    resp = asyncio.run(adapter._neoffice_handle_chat_cancel(
        {"event_type": "chat_cancel", "conversation_id": CID, "request_ids": [RID], "reason": "correction"}))
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["requests"][RID]["outcome"] == "stopped" and _status(conn, tid) == "archived"
    assert R._CANCELLED_REQUESTS[RID][1] == "correction"


@pytest.mark.parametrize("payload", [{}, {"conversation_id": CID}, {"conversation_id": CID, "request_ids": "x"}])
def test_the_webhook_stop_refuses_an_incomplete_order(payload):
    from gateway.platforms import webhook as W

    adapter = W.WebhookAdapter.__new__(W.WebhookAdapter)
    assert asyncio.run(adapter._neoffice_handle_chat_cancel(payload)).status == 400


def test_the_health_says_the_gateway_stops_requests():
    from gateway.platforms import webhook as W

    adapter = W.WebhookAdapter.__new__(W.WebhookAdapter)
    body = json.loads(asyncio.run(adapter._handle_health(None)).body)
    assert "chat_cancel" in body["neoffice_features"]
