"""//// Neoffice — added file (no upstream equivalent).

A chat task out of iteration budget answers with its summary instead of retrying.

Upstream records a `timed_out` failure and releases the claim, so the dispatcher runs
the task again and the summary the model has just written is dropped. On osiris
(2026-09-24) that left a person on the phone with « je réessaie » and five minutes of
silence. A task with a notify subscription is now completed with the summary; a task
nobody subscribed to, or a summary call that failed, keeps upstream's retry.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from agent import turn_finalizer as tf
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn

LOG = logging.getLogger("test")
SUMMARY = "Je n'ai trouvé aucun client « votier ». Vouliez-vous dire la Boulangerie Vautier ?"


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return home


def _task(subscribed: bool) -> str:
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="Facture brouillon de la boulangerie votier", assignee="compta")
        if subscribed:
            kbn.add_notify_sub(conn, task_id=tid, platform="webhook", chat_id="webhook:nora_chat:user:qa@example.com")
        return tid
    finally:
        conn.close()


def _status_and_result(tid: str):
    conn = kbc.connect()
    try:
        row = conn.execute("SELECT status, result FROM tasks WHERE id = ?", (tid,)).fetchone()
        return row[0], row[1]
    finally:
        conn.close()


def test_a_chat_task_is_completed_with_its_summary(board):
    tid = _task(subscribed=True)
    assert tf._neoffice_answer_exhausted_chat_task(tid, SUMMARY, LOG) is True
    assert _status_and_result(tid) == ("done", SUMMARY)


def test_a_task_nobody_waits_on_keeps_the_retry(board):
    tid = _task(subscribed=False)
    assert tf._neoffice_answer_exhausted_chat_task(tid, SUMMARY, LOG) is False
    assert _status_and_result(tid)[0] != "done"


@pytest.mark.parametrize("text", [
    "",
    None,
    "I reached the iteration limit and couldn't generate a summary.",
    "I reached the maximum iterations (25) but couldn't summarize. Error: timeout",
])
def test_no_usable_summary_keeps_the_retry(board, text):
    tid = _task(subscribed=True)
    assert tf._neoffice_answer_exhausted_chat_task(tid, text, LOG) is False
    assert _status_and_result(tid)[0] != "done"
