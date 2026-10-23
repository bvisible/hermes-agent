# //// Neoffice — added file (no upstream equivalent): no pole knows what the open document is in the ERP, so
# //// kanban_create is refused in an editor turn NORA's pre-router decided asks it (23.10).
"""« À qui est cette lettre, et à quoi est-elle reliée dans l'ERP ? », asked from a document that is no Drive file the
person may read, went to support with the instruction in the turn and `drive_file` saying « unknown, do not hand the
question to a pole ». In that turn (marked by the id of the message the router routed) kanban_create is refused with
what to do instead; another turn, another platform and a worker's own child task are untouched."""
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
    monkeypatch.setattr(nora_chat_router, "_DOC_LINK_TURNS", {})
    session_context.reset_session_vars()
    session_context.set_session_vars(platform="webhook", chat_id=CHAT, message_id="delivery-1")
    yield nora_chat_router
    session_context.reset_session_vars()


def _create():
    from tools import kanban_tools as kt
    return json.loads(kt._handle_create({"title": "ERP link of the document", "assignee": "support",
                                         "body": "Which record is « Lettre.docx » linked to?"}))


def test_kanban_create_is_refused_when_asked_what_the_open_document_is(board):
    board._mark_doc_link_turn("delivery-1")
    out = _create()
    assert out.get("ok") is not True, out
    text = json.dumps(out, ensure_ascii=False)
    assert "drive_file" in text and "search_documents" in text and "Do not call kanban_create again" in text


def test_another_turn_creates_tasks(board):
    from gateway import session_context
    board._mark_doc_link_turn("delivery-1")
    session_context.set_session_vars(platform="webhook", chat_id=CHAT, message_id="delivery-2")
    assert _create()["ok"] is True


def test_another_platform_and_a_workers_child_are_untouched(board, monkeypatch):
    from gateway import session_context
    from tools import kanban_tools as kt
    board._mark_doc_link_turn("delivery-1")
    assert kt._neoffice_deck_turn_rejection() == board.DOC_LINK_TURN_REFUSAL
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_0000aaaa")
    monkeypatch.setattr(kt, "_is_dispatcher_owned_worker", lambda: True)
    assert kt._neoffice_deck_turn_rejection() is None
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    session_context.set_session_vars(platform="cli", chat_id=CHAT, message_id="delivery-1")
    assert _create()["ok"] is True


@pytest.mark.parametrize("mark, refusal", [("_mark_deck_turn", "DECK_TURN_REFUSAL"),
                                           ("_mark_sheet_turn", "SHEET_TURN_REFUSAL")])
def test_a_presentation_or_a_workbook_keeps_its_own_refusal(board, mark, refusal):
    getattr(board, mark)("delivery-1")
    board._mark_doc_link_turn("delivery-1")
    from tools import kanban_tools as kt
    assert kt._neoffice_deck_turn_rejection() == getattr(board, refusal)


def test_a_guard_that_fails_never_fails_kanban_create(board, monkeypatch):
    board._mark_doc_link_turn("delivery-1")

    def broken(_message_id):
        raise RuntimeError("the guard broke")

    monkeypatch.setattr(board, "doc_link_turn_under_way", broken)
    assert _create()["ok"] is True


def test_the_mark_lapses_for_kanban_create_too(board, monkeypatch):
    board._mark_doc_link_turn("delivery-1")
    now = board._time_thread.time()
    monkeypatch.setattr(board._time_thread, "time", lambda: now + board._DECK_TURN_TTL + 1)
    assert _create()["ok"] is True
