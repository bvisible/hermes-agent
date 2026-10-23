# //// Neoffice — added file (no upstream equivalent): no pole makes workbooks, so kanban_create is refused in a turn
# //// NORA's pre-router decided is a workbook of figures to make (23.10, step 5 of « Nora partout »).
"""« Fais-moi un classeur des ventes par mois de 2026 » asks for figures a pole would tell in the chat: handed to compta,
no workbook would be made. In that turn (marked by the id of the message the router routed) kanban_create is refused
with what to do instead; another turn, another platform and a worker's own child task are untouched."""
import json

import pytest

CHAT = "webhook:nora_chat:thread:conv-1:user:someone@example.test"


@pytest.fixture
def board(monkeypatch, tmp_path):
    """An orchestrator in the gateway: isolated board, no kanban task of its own, in the turn of delivery-1."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    for name in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)
    from pathlib import Path
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    from gateway import nora_chat_router, session_context
    monkeypatch.setattr(nora_chat_router, "_DECK_TURNS", {})
    monkeypatch.setattr(nora_chat_router, "_SHEET_TURNS", {})
    session_context.reset_session_vars()
    session_context.set_session_vars(platform="webhook", chat_id=CHAT, message_id="delivery-1")
    yield nora_chat_router
    session_context.reset_session_vars()


def _create():
    from tools import kanban_tools as kt
    return json.loads(kt._handle_create({"title": "Sales by month", "assignee": "compta", "body": "Sales of 2026."}))


def test_kanban_create_is_refused_in_a_workbook_turn(board):
    board._mark_sheet_turn("delivery-1")
    out = _create()
    assert out.get("ok") is not True, out
    assert "nora_sheet_create" in json.dumps(out, ensure_ascii=False)


def test_another_turn_creates_tasks(board):
    from gateway import session_context
    board._mark_sheet_turn("delivery-1")
    session_context.set_session_vars(platform="webhook", chat_id=CHAT, message_id="delivery-2")
    assert _create()["ok"] is True


def test_another_platform_and_a_workers_child_are_untouched(board, monkeypatch):
    from gateway import session_context
    from tools import kanban_tools as kt
    board._mark_sheet_turn("delivery-1")
    assert kt._neoffice_deck_turn_rejection() == board.SHEET_TURN_REFUSAL
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_0000aaaa")
    monkeypatch.setattr(kt, "_is_dispatcher_owned_worker", lambda: True)
    assert kt._neoffice_deck_turn_rejection() is None
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    session_context.set_session_vars(platform="cli", chat_id=CHAT, message_id="delivery-1")
    assert _create()["ok"] is True


def test_a_presentation_turn_keeps_its_own_refusal(board):
    board._mark_deck_turn("delivery-1")
    board._mark_sheet_turn("delivery-1")
    from tools import kanban_tools as kt
    assert kt._neoffice_deck_turn_rejection() == board.DECK_TURN_REFUSAL
