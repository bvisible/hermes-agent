# //// Neoffice — added file (no upstream equivalent): the question a card ends on when the loop guardrail stopped its
# //// worker and the worker gave no answer (neoffice-maintenance#3154).
"""On the accounting exam of 19.11, six cards ended done on upstream's stop text, in English: the worker read the same
doctrine section five times, the identical-call streak stopped it, and asked twice for its answer it called the same
tool again. The board, the exam and the poles' film kept that text; the person read the delivery filter's generic
sentence. The card now ends on the catalog's guardrail_card question, in the task's language, and nothing else changes:
the same guardrail, the same summary requests, the same auto-complete net, the same delivery.

The real path: a real board in a temporary home, the real agent loop with its provider faked, the real guardrail, the
real auto-complete net and kanban handlers, then the notifier's formatter and the webhook adapter's real send().
"""

from __future__ import annotations

import json
import pathlib
from unittest.mock import patch

import pytest

from tests.agent.test_tool_call_guardrail_runtime import (
    _hard_stop_config,
    _make_agent,
    _mock_response,
    _mock_tool_call,
)

TOOL = "mcp__neoffice_wiki__wiki_read"
ARGS = json.dumps({"path": "wiki/concepts/a-page.md", "section": "2.1 A section the page does not hold"})
NOT_FOUND = "section introuvable : 2.1 A section the page does not hold\n\nSections disponibles :\n1. Another one"
MARKERS = {"fr": "French", "de": "German", "it": "Italian", "en": "English"}


def _body(lang):
    marker = f"\n\n[Reply to the user in {MARKERS[lang]}. Keep it short.]" if lang else ""
    return f"Comment comptabiliser la part privée d'un logement ?{marker}"


@pytest.fixture
def board(monkeypatch, tmp_path):
    """An isolated board; ``board(body)`` makes one claimed card whose worker this process is."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "compta")
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    kb._INITIALIZED_PATHS.clear()
    kb.init_db()

    def card(body):
        conn = kbc.connect()
        try:
            tid = kb.create_task(conn, title="part privée d'un logement", body=body, assignee="compta")
            kb.claim_task(conn, tid)
            run_id = kb.get_task(conn, tid).current_run_id
        finally:
            conn.close()
        monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
        return tid

    return card


def _read(tid):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    conn = kbc.connect()
    try:
        task = kb.get_task(conn, tid)
        kinds = [row[0] for row in conn.execute("SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (tid,))]
        summary = conn.execute("SELECT summary FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
                               (tid,)).fetchone()
    finally:
        conn.close()
    return task, kinds, (summary[0] if summary else None)


def _stopped_worker(calls=7, **guardrails):
    """The real loop: five identical reads with the same result, the streak's stop, two summary requests answered
    with the same read again (what the exam's workers did; a chat turn makes none). Returns the turn's result."""
    config = _hard_stop_config(**guardrails)
    agent = _make_agent(TOOL, max_iterations=10, config=config)
    agent._disable_streaming = True
    agent._ensure_primary_openai_client = lambda *, reason: agent.client
    reads = [_mock_response(content="", finish_reason="tool_calls", tool_calls=[_mock_tool_call(TOOL, ARGS, f"c{i}")])
             for i in range(1, 6)]
    asked = [_mock_response(content="", finish_reason="tool_calls", tool_calls=[_mock_tool_call(TOOL, ARGS, f"s{i}")])
             for i in range(1, 3)]
    agent.client.chat.completions.create.side_effect = reads + asked
    with (
        patch("model_tools.handle_function_call", return_value=NOT_FOUND),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("work kanban task")
    assert agent.client.chat.completions.create.call_count == calls, "five reads, then the summary requests"
    return result


def _upstream_text_only(monkeypatch):
    """The tree as it was: no card question, upstream's stop text (what 074c2e2b0a does)."""
    from agent import kanban_turn_recovery

    monkeypatch.setattr(kanban_turn_recovery, "neoffice_guardrail_card_question", lambda agent: "", raising=False)


# ── the card ends on the question, keys off ─────────────────────────────────────────────────

def test_a_stopped_card_ends_done_on_the_question_through_the_real_loop(board):
    from neoffice_branding import reply

    tid = board(_body("fr"))
    result = _stopped_worker()
    assert result["turn_exit_reason"] == "guardrail_halt"
    assert result["guardrail"]["code"] == "identical_call_streak_halt"
    task, _kinds, summary = _read(tid)
    assert task.status == "done", "keys off, the auto-complete net still closes the card"
    assert "stopped retrying" not in summary and "`continue`" not in summary, summary
    assert result["final_response"] == summary == reply("guardrail_card", "fr")


def test_keys_off_only_the_text_changes(board, monkeypatch):
    """The same run with upstream's text, then with the question: same status, same events, same exit, same stop."""
    from neoffice_branding import reply

    tid = board(_body("fr"))
    now = _stopped_worker()
    now_task, now_kinds, now_summary = _read(tid)
    assert not now_summary.startswith("I stopped retrying"), now_summary

    _upstream_text_only(monkeypatch)
    before_tid = board(_body("fr"))
    before = _stopped_worker()
    before_task, before_kinds, before_summary = _read(before_tid)

    assert before_summary.startswith("I stopped retrying because I kept running")
    assert now_summary == reply("guardrail_card", "fr")
    assert (now_task.status, now_kinds) == (before_task.status, before_kinds) == ("done", now_kinds)
    assert (now["turn_exit_reason"], now["guardrail"]) == (before["turn_exit_reason"], before["guardrail"])
    assert now.get("failed") == before.get("failed") and now.get("completed") == before.get("completed")


@pytest.mark.asyncio
async def test_the_person_reads_the_question_through_the_notifier_and_the_real_send(board):
    """The notifier's completed line, then the webhook adapter's send(): the question reaches the desk chat whole,
    where upstream's text reached it as the filter's generic guardrail_halt sentence."""
    from types import SimpleNamespace

    from gateway.kanban_watchers_notifier import _neoffice_fmt_completed
    from neoffice_branding import reply
    from tests.gateway.test_neoffice_webhook_status_bubbles import DESK, _adapter, _remember

    tid = board(_body("fr"))
    _stopped_worker()
    task, _kinds, summary = _read(tid)
    message, _wake, _review = _neoffice_fmt_completed(SimpleNamespace(payload={}),
                                                      SimpleNamespace(d={"full_summary": summary}, task=task,
                                                                      title=task.title))
    adapter = _adapter()
    _remember(adapter, DESK, "nora_chat")
    await adapter.send(DESK, message)
    delivered = [a for call in adapter._deliver_nora.await_args_list for a in (*call.args, *call.kwargs.values())
                 if isinstance(a, str)]
    assert delivered and not any(reply("guardrail_halt", "fr") in text for text in delivered), delivered
    assert any(reply("guardrail_card", "fr") in text for text in delivered), delivered


# ── the language ────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("lang", ["fr", "de", "it", "en"])
def test_the_question_is_in_the_language_the_router_gave_the_task(board, lang):
    from neoffice_branding import reply

    tid = board(_body(lang))
    final = _stopped_worker()["final_response"]
    assert not final.startswith("I stopped retrying"), final
    assert final == _read(tid)[2] == reply("guardrail_card", lang)


def test_without_a_language_line_the_question_is_french(board):
    from agent.kanban_turn_recovery import neoffice_task_language

    tid = board(_body(None))
    assert neoffice_task_language(tid) == "fr"


def test_an_unreadable_board_falls_back_to_french(board, monkeypatch, caplog):
    from agent.kanban_turn_recovery import neoffice_task_language
    from hermes_cli import kanban_db_connect

    tid = board(_body("de"))

    def broken(**_kw):
        raise OSError("the board is gone")

    monkeypatch.setattr(kanban_db_connect, "connect", broken)
    assert neoffice_task_language(tid) == "fr"
    assert any("French is used" in r.getMessage() for r in caplog.records)


# ── the key of #3016 on, a chat turn, a fork turn ──────────────────────────────────────────

def test_with_the_key_of_3016_the_card_is_blocked_on_the_same_question(board):
    from gateway.kanban_watchers import _safe_accounting_block_reason
    from neoffice_branding import reply

    tid = board(_body("fr"))
    result = _stopped_worker(neoffice_final_answer_without_tools=True)
    task, kinds, _summary = _read(tid)
    assert task.status == "blocked" and "blocked" in kinds and "completed" not in kinds
    assert not result["final_response"].startswith("I stopped retrying"), "the block's reason is what the person reads"
    question = reply("guardrail_card", "fr")
    assert result["final_response"] == question
    # A compta card about an account keeps only the questions of its block reason: this one survives whole.
    assert _safe_accounting_block_reason(question, assignee="compta", title="dans quel compte imputer ?") == question


def test_a_chat_turn_keeps_upstreams_text_which_the_delivery_filter_replaces(monkeypatch):
    from neoffice_branding import reply, strip_internal_mechanics

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    final = _stopped_worker(calls=5)["final_response"]
    assert final.startswith("I stopped retrying because I kept running")
    assert strip_internal_mechanics(final, "fr") == reply("guardrail_halt", "fr")


def test_a_fork_turn_is_not_given_the_question(board):
    from types import SimpleNamespace

    from agent.kanban_turn_recovery import neoffice_guardrail_card_question

    board(_body("fr"))
    assert neoffice_guardrail_card_question(SimpleNamespace(_turn_origin="skill_review")) == ""
    assert neoffice_guardrail_card_question(SimpleNamespace()) != ""


# ── the sentence ────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("lang", ["fr", "de", "it", "en"])
def test_the_question_is_one_question_with_no_colon_and_no_number(lang):
    from neoffice_branding import reply, strip_internal_mechanics

    text = reply("guardrail_card", lang)
    assert text.endswith("?") and text.count("?") == 1, "the accounting chat keeps the questions of a block reason"
    assert ":" not in text, "the chat line already reads « ✋ <pole> : <question> »"
    assert not any(ch.isdigit() for ch in text) and "." not in text and "!" not in text
    assert strip_internal_mechanics(text, lang) == text, "the delivery filter leaves it as it is"
