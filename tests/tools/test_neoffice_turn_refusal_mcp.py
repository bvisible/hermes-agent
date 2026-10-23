# //// Neoffice — added file (no upstream equivalent): a neoffice-* MCP tool the pre-router's turn does not allow is
# //// refused before the call leaves (23.10, a question about a workbook makes none).
"""« Est-il possible d'exporter les ventes en Excel ? » only asks whether a workbook can be made, yet the orchestrator,
holding nora_sheet_create, made one. In that turn (marked by the id of the message the router routed) the call is
refused with what to do instead, before any server is reached; another tool, another turn, another server and another
platform are untouched."""
import json

import pytest

import tools.mcp_tool_handlers as handlers

CHAT = "webhook:nora_chat:thread:conv-1:user:someone@example.test"


@pytest.fixture
def turn(monkeypatch):
    """The orchestrator in the gateway, in the turn of delivery-1."""
    from gateway import nora_chat_router, session_context

    monkeypatch.setattr(nora_chat_router, "_SHEET_QUESTIONS", {})
    session_context.reset_session_vars()
    session_context.set_session_vars(platform="webhook", chat_id=CHAT, message_id="delivery-1", user_id="someone@example.test")
    yield nora_chat_router
    session_context.reset_session_vars()


def test_the_workbook_tool_is_refused_in_a_question_turn(turn):
    turn._remember(turn._SHEET_QUESTIONS, "delivery-1")
    assert handlers._neoffice_turn_refusal("neoffice-tasks", "nora_sheet_create") == turn.SHEET_QUESTION_TOOL_REFUSAL
    assert handlers._neoffice_turn_refusal("neoffice-tasks", "nora_company_details") is None
    assert handlers._neoffice_turn_refusal("other-server", "nora_sheet_create") is None


def test_another_turn_and_another_platform_are_untouched(turn):
    from gateway import session_context

    turn._remember(turn._SHEET_QUESTIONS, "delivery-1")
    session_context.set_session_vars(platform="webhook", chat_id=CHAT, message_id="delivery-2")
    assert handlers._neoffice_turn_refusal("neoffice-tasks", "nora_sheet_create") is None
    session_context.set_session_vars(platform="cli", chat_id=CHAT, message_id="delivery-1")
    assert handlers._neoffice_turn_refusal("neoffice-tasks", "nora_sheet_create") is None


def test_the_refused_call_never_reaches_a_server(turn, monkeypatch):
    turn._remember(turn._SHEET_QUESTIONS, "delivery-1")

    def no_server(*_a, **_kw):
        pytest.fail("a refused call never reaches its server")

    monkeypatch.setattr(handlers, "_acquire_call_server", no_server)
    monkeypatch.setattr(handlers, "_trust_gate_check", lambda *a: None)
    monkeypatch.setattr(handlers, "_check_circuit_breaker", lambda *a: None)
    out = json.loads(handlers._make_tool_handler("neoffice-tasks", "nora_sheet_create", 30)({"source": "sales_by_month"}))
    assert "nothing is made in this turn" in json.dumps(out, ensure_ascii=False)


def test_a_broken_router_never_fails_the_call(turn, monkeypatch):
    def broken(*_a, **_kw):
        raise RuntimeError("router state unreadable")

    monkeypatch.setattr(turn, "tool_refused_in_turn", broken)
    assert handlers._neoffice_turn_refusal("neoffice-tasks", "nora_sheet_create") is None
