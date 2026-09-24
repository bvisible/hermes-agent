# //// Neoffice — added file (no upstream equivalent): the kanban auto-complete net says when
# //// the completion it attempted was refused, instead of logging "completed" regardless.
"""A refused completion leaves the card running, and the dispatcher respawns the task.

Seen on the dev instance (2026-09-24): a compta worker halted by the tool guardrail was
"completed" by the net in the log, while our evidence guard had refused the completion.
The dispatcher counted a protocol violation and ran the same loop twice more.
"""

from __future__ import annotations

import json
import logging

import pytest

from tests.agent.test_neoffice_worker_skill_review import (  # noqa: F401 — fixtures
    _agent_with_review,
    review_slot,
    worker_card,
)
from agent.turn_finalizer import finalize_turn


def _finalize_with_text(agent):
    finalize_turn(
        agent, final_response="Voici les lignes et leurs comptes.", api_call_count=6, interrupted=False,
        failed=False, messages=[{"role": "assistant", "content": "Voici les lignes et leurs comptes."}],
        conversation_history=[], effective_task_id="worker", turn_id="worker-turn",
        user_message="work kanban task", original_user_message="work kanban task",
        _should_review_memory=False, _turn_exit_reason="text_response(finish_reason=stop)",
    )


@pytest.mark.parametrize("refused", [True, False])
def test_the_net_says_whether_its_completion_went_through(worker_card, monkeypatch, caplog, refused):
    import threading

    from tools import kanban_tools

    def complete(args, **_kw):
        if refused:
            return json.dumps({"error": "kanban_complete blocked: the doctrine was not checked"})
        return json.dumps({"ok": True, "task_id": worker_card})

    monkeypatch.setattr(kanban_tools, "_handle_complete", complete)
    agent = _agent_with_review(0.0, threading.Event())
    agent._iters_since_skill = 0  # no review in this test

    with caplog.at_level(logging.INFO, logger="agent.conversation_loop"):
        _finalize_with_text(agent)

    lines = [r.getMessage() for r in caplog.records if "auto-complete net" in r.getMessage()]
    if refused:
        assert any("REFUSED" in line and "doctrine" in line for line in lines), lines
        assert not any("completed" in line and "REFUSED" not in line for line in lines), lines
    else:
        assert any("completed" in line for line in lines) and not any("REFUSED" in line for line in lines), lines

