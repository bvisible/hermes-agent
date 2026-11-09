# //// Neoffice — added file (no upstream equivalent): a chat task's recent work is its own conversation's (#2376).
"""A NORA pole serves every person and every conversation. The worker's context listed the pole's last five tasks
whoever they were for: a worker took another conversation's job (the same words, another customer) for the one it was
asked about, then booked and invoiced it, and it read the summaries of work done for other people (09.11).
"""
from pathlib import Path

import pytest

A, B = "nora-aaaa000000000000", "nora-bbbb000000000000"


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


def _done(kb, conn, title, key, summary):
    tid = kb.create_task(conn, title=title, assignee="projet", idempotency_key=key, initial_status="running")
    assert kb.complete_task(conn, tid, summary=summary, force=True)
    return tid


def _context(kb, conn, key):
    tid = kb.create_task(conn, title="Prévois 1h30", assignee="projet", idempotency_key=key, initial_status="running")
    return kb.build_worker_context(conn, tid)


def test_a_chat_task_reads_only_its_own_conversations_recent_work(board):
    kb, conn = board
    _done(kb, conn, "Dépannage chez Wuilloud", f"{A}:1794242045000", "Projet ouvert pour Edmond Wuilloud")
    _done(kb, conn, "Dépannage chez Blanc", f"{B}:1794242000000", "Projet ouvert pour Huguette Blanc")
    context = _context(kb, conn, f"{A}:1794242099000")
    assert "Edmond Wuilloud" in context
    assert "Huguette Blanc" not in context


def test_a_conversation_with_no_earlier_work_reads_none(board):
    kb, conn = board
    _done(kb, conn, "Dépannage chez Blanc", f"{B}:1794242000000", "Projet ouvert pour Huguette Blanc")
    context = _context(kb, conn, f"{A}:1794242099000")
    assert "Recent work" not in context and "Huguette Blanc" not in context


def test_a_task_of_no_conversation_keeps_the_roles_continuity(board):
    kb, conn = board
    _done(kb, conn, "Dépannage chez Wuilloud", f"{A}:1794242045000", "Projet ouvert pour Edmond Wuilloud")
    _done(kb, conn, "Dépannage chez Blanc", f"{B}:1794242000000", "Projet ouvert pour Huguette Blanc")
    context = _context(kb, conn, "nightly-review-42")
    assert "Edmond Wuilloud" in context and "Huguette Blanc" in context


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
