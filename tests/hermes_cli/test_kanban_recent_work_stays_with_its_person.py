# //// Neoffice — added file (no upstream equivalent): a task's recent work is its own person's (#3025).
"""A NORA pole serves every person of an instance. The worker's context listed the pole's last five completed tasks
whoever they were for (« Recent work by @<pole> »): a seller asked for a prospect's e-mail and got it; a moment later
another seller, without the right to read that prospect, asked the same thing through a task the root profile
delegated with kanban_create, and the worker repeated the first seller's result to them. #2376 bounded only the chat
router's tasks, by conversation.

Of the rows the section showed, only the work done for the same person is kept: nothing is shown that was not shown
before. A task is a person's only while every one of its webhook subs names that person (subs are added after a task
ran: a delegation that reuses an idempotency key, a link). A task that names nobody, or several people, reads none.
"""
import sys
import time
from pathlib import Path

import pytest

SELLER, OTHER_SELLER = "seller.one@example.test", "seller.two@example.test"
CONVERSATION = "nora-cccc000000000000"
PROSPECT = "Lead CRM-LEAD-0001, e-mail contact@prospect-one.example"


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
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


def _subscribe(conn, tid, chat_id, user_id=None, platform="webhook"):
    from hermes_cli import kanban_db_notify as kbn

    kbn.add_notify_sub(conn, task_id=tid, platform=platform, chat_id=chat_id, user_id=user_id)


def _chat_id(person, thread="th1"):
    return f"webhook:nora_chat:thread:{thread}:user:{person}"


def _task(kb, conn, title, person=None, key=None, created_by=None, thread="th1"):
    tid = kb.create_task(
        conn, title=title, assignee="ventes", idempotency_key=key, created_by=created_by, initial_status="running"
    )
    if person:
        _subscribe(conn, tid, _chat_id(person, thread))
    return tid


def _done(kb, conn, title, summary, ended_in=None, **kwargs):
    tid = _task(kb, conn, title, **kwargs)
    assert kb.complete_task(conn, tid, summary=summary, force=True)
    if ended_in is not None:  # seconds from now: an order the test chooses (runs ended in the same second tie)
        with kb.write_txn(conn):
            conn.execute("UPDATE task_runs SET ended_at = ? WHERE task_id = ?", (int(time.time()) + ended_in, tid))
    return tid


def _context(kb, conn, **kwargs):
    return kb.build_worker_context(conn, _task(kb, conn, "Lead — e-mail", **kwargs))


# ── the leak ──────────────────────────────────────────────────────────


def test_a_task_the_root_delegates_reads_none_of_another_persons_work(board):
    kb, conn = board
    _done(kb, conn, "Retrouve le lead", PROSPECT, person=SELLER, key=f"{CONVERSATION}:1794242045000",
          created_by="nora-chat-router")
    context = _context(kb, conn, person=OTHER_SELLER, created_by="default")
    assert "prospect-one.example" not in context and "CRM-LEAD-0001" not in context
    assert "Recent work" not in context


def test_a_chat_task_reads_only_its_persons_work_even_in_the_same_conversation(board):
    kb, conn = board
    _done(kb, conn, "Retrouve le lead", PROSPECT, person=SELLER, key=f"{CONVERSATION}:1794242045000")
    context = _context(kb, conn, person=OTHER_SELLER, key=f"{CONVERSATION}:1794242099000")
    assert "CRM-LEAD-0001" not in context


def test_a_done_task_another_person_joined_is_shown_to_neither(board):
    # the root, serving the second seller, delegated with the same idempotency key: create_task returned the first
    # seller's task and subscribed the second seller to it
    kb, conn = board
    joined = _done(kb, conn, "Retrouve le lead", PROSPECT, person=SELLER, created_by="default")
    _subscribe(conn, joined, _chat_id(OTHER_SELLER, "th2"))
    assert "CRM-LEAD-0001" not in _context(kb, conn, person=OTHER_SELLER, created_by="default")
    assert "CRM-LEAD-0001" not in _context(kb, conn, person=SELLER, created_by="default")


def test_a_task_shared_by_two_people_reads_no_recent_work(board):
    kb, conn = board
    _done(kb, conn, "Devis", "Offre Q-0002 envoyée", person=OTHER_SELLER, created_by="default")
    shared = _task(kb, conn, "Lead — e-mail", person=SELLER, created_by="default")
    _subscribe(conn, shared, _chat_id(OTHER_SELLER, "th2"))
    context = kb.build_worker_context(conn, shared)
    assert "Recent work" not in context and "Q-0002" not in context


def test_a_task_done_for_nobody_that_gets_a_sub_later_is_shown_to_no_one(board):
    # a night job done for nobody, later linked from a person's task (the link copies that person's sub)
    kb, conn = board
    night = _done(kb, conn, "Contrôle de nuit", PROSPECT, created_by="default", ended_in=-100)
    _subscribe(conn, night, _chat_id(SELLER))
    assert "CRM-LEAD-0001" not in _context(kb, conn, person=SELLER, created_by="default")


def test_a_task_whose_subs_were_purged_then_resubscribed_is_shown_to_no_one(board):
    # the subs of a done task are purged after 30 days; a delegation reusing its key subscribes a new asker
    kb, conn = board
    old = _done(kb, conn, "Retrouve le lead", PROSPECT, person=OTHER_SELLER, created_by="default", ended_in=-100)
    with kb.write_txn(conn):
        conn.execute("DELETE FROM kanban_notify_subs WHERE task_id = ?", (old,))
    _subscribe(conn, old, _chat_id(SELLER))
    assert "CRM-LEAD-0001" not in _context(kb, conn, person=SELLER, created_by="default")
    assert "CRM-LEAD-0001" not in _context(kb, conn, person=OTHER_SELLER, created_by="default")


def test_a_task_that_names_nobody_reads_no_recent_work(board):
    # a night job, a card, a task created with no subscriber: what it loses is the pole's role continuity
    kb, conn = board
    _done(kb, conn, "Retrouve le lead", PROSPECT, person=SELLER)
    context = _context(kb, conn, created_by="default")
    assert "Recent work" not in context and "CRM-LEAD-0001" not in context


def test_a_person_whose_address_is_within_anothers_reads_none_of_its_work(board):
    kb, conn = board
    _done(kb, conn, "Retrouve le lead", PROSPECT, person="ann@example.test")
    context = _context(kb, conn, person="ann@example.te")
    assert "CRM-LEAD-0001" not in context


# ── what is kept, and nothing more ────────────────────────────────────


def test_a_task_the_root_delegates_reads_its_own_persons_work(board):
    kb, conn = board
    _done(kb, conn, "Devis", "Offre Q-0002 envoyée", person=OTHER_SELLER, created_by="default", ended_in=10)
    _done(kb, conn, "Retrouve le lead", PROSPECT, person=SELLER, created_by="default", ended_in=20)
    context = _context(kb, conn, person=OTHER_SELLER, created_by="default")
    assert "## Recent work by @ventes" in context and "Q-0002" in context
    assert "CRM-LEAD-0001" not in context


def test_the_section_never_reaches_past_the_poles_last_five(board):
    # the rows are only filtered: an older task of the same person, beyond the pole's last five, stays out
    kb, conn = board
    _done(kb, conn, "Devis", "Offre Q-0002 envoyée", person=SELLER, created_by="default", ended_in=10)
    for n in range(5):
        _done(kb, conn, f"Lead {n}", f"Résultat R-{n}", person=OTHER_SELLER, created_by="default", ended_in=20 + n)
    context = _context(kb, conn, person=SELLER, created_by="default")
    assert "Recent work" not in context and "Q-0002" not in context


def test_a_failure_to_read_the_subs_loses_the_section_not_the_worker(board, monkeypatch):
    kb, conn = board
    from hermes_cli import kanban_task_person

    _done(kb, conn, "Devis", "Offre Q-0002 envoyée", person=SELLER, created_by="default")

    def broken(conn, task_id):
        raise RuntimeError("a value the reader cannot handle")

    monkeypatch.setattr(kanban_task_person, "sole_person", broken)
    context = _context(kb, conn, person=SELLER, created_by="default")
    assert "# Kanban task" in context and "Recent work" not in context


# ── the person, read once for the gateway and the context ─────────────


def test_a_task_is_the_person_of_its_latest_webhook_sub_and_a_sole_person_only_alone(board):
    kb, conn = board
    from hermes_cli.kanban_task_person import sole_person, task_person

    tid = _task(kb, conn, "Lead", person=SELLER)
    assert task_person(conn, tid) == sole_person(conn, tid) == SELLER
    _subscribe(conn, tid, "telegram:12345", user_id="third@example.test", platform="telegram")
    assert sole_person(conn, tid) == SELLER  # only webhook subs name the person
    _subscribe(conn, tid, _chat_id(OTHER_SELLER, "th2"))
    assert task_person(conn, tid) == OTHER_SELLER and sole_person(conn, tid) == ""
    nobody = _task(kb, conn, "Night job")
    assert task_person(conn, nobody) == sole_person(conn, nobody) == ""
    unnamed = _task(kb, conn, "Card")
    _subscribe(conn, unnamed, "webhook:whatsapp:41790000000", user_id="41790000000")
    assert sole_person(conn, unnamed) == ""


@pytest.mark.parametrize(
    "chat_id, user_id, person",
    [
        ("webhook:nora_chat:thread:th1:user:a@x.test", None, "a@x.test"),
        ("webhook:nora_chat:thread:user:user:a@x.test", None, "a@x.test"),  # a thread named « user »
        ("webhook:nora_chat:user:a@x.test", None, "a@x.test"),
        ("webhook:nora_chat:thread:th1:user:Administrator", None, "Administrator"),
        ("webhook:nora_chat:thread:th1:conv:c1", "b@x.test", "b@x.test"),
        ("webhook:whatsapp:41790000000", "a@x.test", "a@x.test"),
        ("webhook:whatsapp:41790000000", "41790000000", ""),
        ("", None, ""),
    ],
)
def test_the_person_of_a_sub(chat_id, user_id, person):
    from hermes_cli.kanban_task_person import person_of_sub

    assert person_of_sub(chat_id, user_id) == person


@pytest.fixture
def gateway(board, monkeypatch):
    import tools.mcp_tool_handlers as handlers

    monkeypatch.setattr(handlers, "_TASK_USER_CACHE", {})
    monkeypatch.setattr(handlers, "_TASK_THREAD_CACHE", {})
    return board, handlers


def test_the_gateway_stamps_the_person_the_context_reads(gateway, monkeypatch):
    (kb, conn), handlers = gateway
    from hermes_cli.kanban_task_person import task_person

    tid = _task(kb, conn, "Lead", person=SELLER, thread="th7")
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    assert handlers._kanban_task_user() == task_person(conn, tid) == SELLER
    assert handlers._kanban_task_thread() == "th7"


def test_the_gateway_names_nobody_outside_a_task(gateway):
    _, handlers = gateway
    assert handlers._kanban_task_user() == ""


def test_the_gateway_keeps_no_miss_and_reads_a_sub_written_later(gateway, monkeypatch):
    (kb, conn), handlers = gateway
    tid = _task(kb, conn, "Lead")
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    assert handlers._kanban_task_user() == "" and tid not in handlers._TASK_USER_CACHE
    _subscribe(conn, tid, _chat_id(SELLER))
    assert handlers._kanban_task_user() == SELLER


@pytest.mark.parametrize(
    "chat_id, user_id, person",
    [
        ("webhook:whatsapp:41790000000", SELLER, SELLER),
        ("webhook:nora_chat:thread:th1:user:Administrator", None, "Administrator"),
        ("webhook:whatsapp:41790000000", "41790000000", ""),
    ],
)
def test_the_gateway_reads_each_kind_of_sub(gateway, monkeypatch, chat_id, user_id, person):
    (kb, conn), handlers = gateway
    tid = _task(kb, conn, "Lead")
    _subscribe(conn, tid, chat_id, user_id=user_id)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    assert handlers._kanban_task_user() == person


def test_the_gateway_returns_a_kept_hit_without_reading_the_board(gateway, monkeypatch):
    _, handlers = gateway
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_kept")
    handlers._TASK_USER_CACHE["t_kept"] = SELLER
    assert handlers._kanban_task_user() == SELLER


def test_the_gateway_names_nobody_when_the_reader_cannot_load(gateway, monkeypatch):
    (kb, conn), handlers = gateway
    tid = _task(kb, conn, "Lead", person=SELLER)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setitem(sys.modules, "hermes_cli.kanban_task_person", None)
    assert handlers._kanban_task_user() == "" and handlers._kanban_task_thread() == ""
