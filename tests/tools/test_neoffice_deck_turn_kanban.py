# //// Neoffice — added file (no upstream equivalent): no pole makes presentations, so kanban_create is refused in a
# //// turn NORA's pre-router decided is a presentation to make (23.10, essay on osiris).
"""« Fais-moi une présentation de 3 diapositives sur nos horaires » was handed by the orchestrator to rh, which reads no
opening hours and makes no presentation: the person got rh's question and no deck. In that turn (marked by the id of
the message the router routed) kanban_create is refused with what to do instead; another turn, even of the same
thread, another platform and a worker's own child task are untouched."""
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
    session_context.reset_session_vars()
    session_context.set_session_vars(platform="webhook", chat_id=CHAT, message_id="delivery-1")
    yield nora_chat_router
    session_context.reset_session_vars()


def _create():
    from tools import kanban_tools as kt
    return json.loads(kt._handle_create({"title": "Opening hours", "assignee": "rh", "body": "Find the hours."}))


def test_kanban_create_is_refused_in_a_presentation_turn(board):
    board._mark_deck_turn("delivery-1")
    out = _create()
    assert out.get("ok") is not True, out
    assert "nora_presentation_create" in json.dumps(out, ensure_ascii=False)


def test_another_turn_of_the_same_thread_creates_tasks(board):
    # A second message of the thread (a second tab) is its own turn: the presentation's mark is not its.
    from gateway import session_context
    board._mark_deck_turn("delivery-1")
    session_context.set_session_vars(platform="webhook", chat_id=CHAT, message_id="delivery-2")
    assert _create()["ok"] is True


def test_a_turn_without_a_message_id_creates_tasks(board):
    from gateway import session_context
    board._mark_deck_turn("delivery-1")
    session_context.set_session_vars(platform="webhook", chat_id=CHAT)
    assert _create()["ok"] is True


def test_another_platform_is_untouched(board):
    from gateway import session_context
    board._mark_deck_turn("delivery-1")
    session_context.set_session_vars(platform="cli", chat_id=CHAT, message_id="delivery-1")
    assert _create()["ok"] is True


def test_a_workers_own_child_task_is_never_refused(board, monkeypatch):
    # kanban_tools.py: a dispatcher-owned worker creates its children; the presentation's mark is not its business.
    from tools import kanban_tools as kt
    board._mark_deck_turn("delivery-1")
    assert kt._neoffice_deck_turn_rejection() == board.DECK_TURN_REFUSAL
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_0000aaaa")
    monkeypatch.setattr(kt, "_is_dispatcher_owned_worker", lambda: True)
    assert kt._neoffice_deck_turn_rejection() is None
    # The same variable inherited by something that is no dispatcher-owned worker is no exemption.
    monkeypatch.setattr(kt, "_is_dispatcher_owned_worker", lambda: False)
    assert kt._neoffice_deck_turn_rejection() == board.DECK_TURN_REFUSAL
