# //// Neoffice — added file (no upstream equivalent): a board removed since the tick listed it is skipped quietly.
"""The dispatcher lists the boards, then opens each. A board removed in between (a test run removes and recreates
its board) has neither its database nor its board.json any more: connect() refuses to resurrect it and the tick
logged « tick failed on board … does not exist » with a traceback (development instance, 09.10). Such a board is
skipped for that tick, without an error and without creating anything. A live board whose database is missing (its
board.json is there) still opens and gets its database back, as upstream intends; so does the default board."""
import logging

from gateway.kanban_watchers_dispatcher import _KanbanDispatcher, _resolve_dispatcher_settings
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_boards import write_board_metadata


def _dispatcher():
    return _KanbanDispatcher(kb, _resolve_dispatcher_settings({}, kb))


def _db_path(slug):
    with kb.pin_first_board_resolution():
        return kb.kanban_db_path(slug)


def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)


def test_a_board_gone_since_it_was_listed_is_skipped_quietly(tmp_path, monkeypatch, caplog):
    _isolated(tmp_path, monkeypatch)
    assert not _db_path("gone-board").exists() and not kb.board_metadata_path("gone-board").exists()
    with caplog.at_level(logging.DEBUG, logger="gateway.run"):
        assert _dispatcher().tick_once_for_board("gone-board") is None
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == []
    assert not _db_path("gone-board").exists()  # nothing was created on its behalf


def test_a_live_board_without_its_database_still_opens(tmp_path, monkeypatch, caplog):
    _isolated(tmp_path, monkeypatch)
    write_board_metadata("live-board", name="live board")
    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        assert _dispatcher().tick_once_for_board("live-board") is not None
    assert _db_path("live-board").exists()
    assert [r.getMessage() for r in caplog.records if "tick failed" in r.getMessage()] == []


def test_the_default_board_still_opens(tmp_path, monkeypatch, caplog):
    _isolated(tmp_path, monkeypatch)
    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        _dispatcher().tick_once_for_board(kb.DEFAULT_BOARD)
    assert _db_path(kb.DEFAULT_BOARD).exists()
    assert [r.getMessage() for r in caplog.records if "tick failed" in r.getMessage()] == []
