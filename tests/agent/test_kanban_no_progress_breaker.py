"""Unit tests for the kanban worker no-progress / narration-loop breaker.

Covers the two PURE pieces of the breaker (agent/conversation_loop.py):
  - ``_classify_worker_turn_progress``: terminal / progress / non_progress
  - ``_kanban_no_progress_decision``:   count → nudge@2 → block@3

The full loop integration (nudge injected, kanban_block forced, loop broken
before max_iterations, no-op when HERMES_KANBAN_TASK is unset) is exercised
end-to-end by the live desk usage suite (neoffice-devops tests/llm/25 — repeat-N
+ assert_worker_no_loop). This file locks the deterministic decision table so the
nudge/block thresholds can't drift unnoticed.
"""
from types import SimpleNamespace

from agent.conversation_loop import (
    _classify_worker_turn_progress,
    _kanban_no_progress_decision,
)


def _msg(*tool_names):
    """Stub assistant_message with the given tool-call names (none → text-only)."""
    return SimpleNamespace(
        tool_calls=[
            SimpleNamespace(function=SimpleNamespace(name=n)) for n in tool_names
        ]
    )


class TestClassifyWorkerTurnProgress:
    def test_text_only_is_non_progress(self):
        assert _classify_worker_turn_progress(_msg()) == "non_progress"

    def test_none_tool_calls_is_non_progress(self):
        assert (
            _classify_worker_turn_progress(SimpleNamespace(tool_calls=None))
            == "non_progress"
        )

    def test_kanban_comment_only_is_non_progress(self):
        assert _classify_worker_turn_progress(_msg("kanban_comment")) == "non_progress"

    def test_kanban_show_only_is_non_progress(self):
        assert _classify_worker_turn_progress(_msg("kanban_show")) == "non_progress"

    def test_housekeeping_combo_is_non_progress(self):
        assert (
            _classify_worker_turn_progress(
                _msg("kanban_comment", "kanban_show", "memory")
            )
            == "non_progress"
        )

    def test_business_tool_is_progress(self):
        assert (
            _classify_worker_turn_progress(_msg("frappe_payment_reminder_create"))
            == "progress"
        )

    def test_business_tool_with_comment_is_progress(self):
        # A real tool anywhere in the turn = progress (the comment is incidental).
        assert (
            _classify_worker_turn_progress(
                _msg("kanban_comment", "frappe_revenue_summary")
            )
            == "progress"
        )

    def test_kanban_complete_is_terminal(self):
        assert _classify_worker_turn_progress(_msg("kanban_complete")) == "terminal"

    def test_kanban_block_is_terminal(self):
        assert _classify_worker_turn_progress(_msg("kanban_block")) == "terminal"

    def test_terminal_wins_over_comment(self):
        assert (
            _classify_worker_turn_progress(_msg("kanban_comment", "kanban_complete"))
            == "terminal"
        )


class TestNoProgressDecision:
    def test_first_non_progress_turn_counts(self):
        assert _kanban_no_progress_decision(streak=1, nudged=False) == "count"

    def test_second_non_progress_turn_nudges(self):
        assert _kanban_no_progress_decision(streak=2, nudged=False) == "nudge"

    def test_third_non_progress_turn_blocks_after_nudge(self):
        assert _kanban_no_progress_decision(streak=3, nudged=True) == "block"

    def test_just_nudged_keeps_counting(self):
        # streak=2, nudge just spent → not yet block (needs streak>=3).
        assert _kanban_no_progress_decision(streak=2, nudged=True) == "count"

    def test_never_blocks_before_a_nudge(self):
        # Defensive: a high streak with the nudge never spent must nudge, not
        # block — we always give one corrective chance before force-blocking.
        assert _kanban_no_progress_decision(streak=5, nudged=False) == "nudge"
