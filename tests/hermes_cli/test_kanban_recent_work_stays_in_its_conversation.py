# //// Neoffice — added file (no upstream equivalent): a chat task's recent work is its own conversation's (#2376).
"""A NORA pole serves every person and every conversation. The worker's context listed the pole's last five tasks
whoever they were for: a worker took another conversation's job (the same words, another customer) for the one it was
asked about, then booked and invoiced it, and it read the summaries of work done for other people (09.11).

Since #3025 the recent work is also the same person's: every task here names its person through a webhook notify sub,
as the chat router writes it (tests of the person: test_kanban_recent_work_stays_with_its_person.py).
"""
from pathlib import Path

import pytest

A, B = "nora-aaaa000000000000", "nora-bbbb000000000000"
PERSON, OTHER = "anna.muster@example.test", "bruno.beispiel@example.test"


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


def _subscribe(conn, tid, person, thread="th1"):
    from hermes_cli import kanban_db_notify as kbn

    kbn.add_notify_sub(conn, task_id=tid, platform="webhook", chat_id=f"webhook:nora_chat:thread:{thread}:user:{person}")


def _done(kb, conn, title, key, summary, person=PERSON):
    tid = kb.create_task(conn, title=title, assignee="projet", idempotency_key=key, initial_status="running")
    _subscribe(conn, tid, person)
    assert kb.complete_task(conn, tid, summary=summary, force=True)
    return tid


def _context(kb, conn, key, person=PERSON):
    tid = kb.create_task(conn, title="Prévois 1h30", assignee="projet", idempotency_key=key, initial_status="running")
    _subscribe(conn, tid, person)
    return kb.build_worker_context(conn, tid)


def test_a_chat_task_reads_only_its_own_conversations_recent_work(board):
    kb, conn = board
    _done(kb, conn, "Dépannage chez Exemplar", f"{A}:1794242045000", "Projet ouvert pour Carla Exemplar")
    _done(kb, conn, "Dépannage chez Fictiva", f"{B}:1794242000000", "Projet ouvert pour Dora Fictiva")
    context = _context(kb, conn, f"{A}:1794242099000")
    assert "Carla Exemplar" in context
    assert "Dora Fictiva" not in context


def test_a_conversation_with_no_earlier_work_reads_none(board):
    kb, conn = board
    _done(kb, conn, "Dépannage chez Fictiva", f"{B}:1794242000000", "Projet ouvert pour Dora Fictiva")
    context = _context(kb, conn, f"{A}:1794242099000")
    assert "Recent work" not in context and "Dora Fictiva" not in context


def test_a_task_of_no_conversation_reads_its_persons_work_and_nobody_elses(board):
    # #2376 kept upstream's role continuity here: the pole's last five tasks, whoever they were for (#3025)
    kb, conn = board
    _done(kb, conn, "Dépannage chez Exemplar", f"{A}:1794242045000", "Projet ouvert pour Carla Exemplar")
    _done(kb, conn, "Dépannage chez Fictiva", f"{B}:1794242000000", "Projet ouvert pour Dora Fictiva", person=OTHER)
    context = _context(kb, conn, "nightly-review-42")
    assert "Carla Exemplar" in context and "Dora Fictiva" not in context


@pytest.mark.parametrize(
    "key, conversation",
    [
        (f"{A}:1794242045000", A),
        ("k", ""),
        (None, ""),
        (f"{A}:abc", ""),
        ("whatsapp-41790000000:1794242045000", "whatsapp-41790000000"),
    ],
)
def test_the_conversation_is_read_from_the_routers_request_key(key, conversation):
    from hermes_cli.kanban_db_context import _chat_conversation

    assert _chat_conversation(key) == conversation
