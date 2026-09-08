"""Kanban worker no-progress / narration-loop breaker.

# //// Neoffice — added file (no upstream equivalent).
#
# Before the Sep 2026 decomposition this lived inline in
# ``agent/conversation_loop.py``, inside the tool-call branch of the turn loop.
# Upstream split that loop into ``agent/turn_*.py`` phase helpers driven by
# ``_run_phase``, so our breaker moves here and follows the same contract: it
# takes the loop locals it names and returns a verdict whose fields are copied
# back into the loop state.
#
# What it does and why: a weak model (Qwen/Gemma) on a kanban worker loops
# calling ``kanban_comment`` to narrate its intent without ever calling the
# business tool. After 2 consecutive non-progress turns we inject ONE corrective
# nudge; if the 3rd is still non-progress we force ``kanban_block`` and end the
# run — otherwise the loop burns the whole iteration budget and the
# auto-complete net in ``cli.py`` delivers an English mid-thought to a French
# customer (forcing the block flips the status off ``running``, so that net
# no-ops). Reset-on-progress keeps legitimate work safe. Worker-only, gated on
# ``HERMES_KANBAN_TASK`` — zero effect on the orchestrator or interactive chat.
#
# Drop this module if upstream ever ships a progress breaker for dispatched
# workers; until then ``run_conversation`` calls it once per tool round.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class KanbanBreakerVerdict:
    """``action``: ``"fallthrough"`` (nothing to do — honour the tool round's own
    verdict), ``"continue"`` (a corrective nudge was appended, re-issue the
    iteration) or ``"break"`` (the task was force-blocked, or a terminal kanban
    tool already moved it off ``running``, so the run ends)."""

    action: str
    messages: Any
    final_response: Any
    _turn_exit_reason: Any


def neoffice_kanban_no_progress_breaker(
    agent: Any, *, assistant_message: Any, messages: Any, final_response: Any,
    _turn_exit_reason: Any,
) -> KanbanBreakerVerdict:
    """Classify this worker turn and break the narration loop when it stops progressing."""
    from agent.conversation_loop import (
        _classify_worker_turn_progress,
        _kanban_no_progress_decision,
    )

    def _verdict(action: str) -> KanbanBreakerVerdict:
        return KanbanBreakerVerdict(
            action=action, messages=messages, final_response=final_response,
            _turn_exit_reason=_turn_exit_reason,
        )

    kanban_task = os.environ.get("HERMES_KANBAN_TASK")
    if not kanban_task:
        return _verdict("fallthrough")

    # Defensive: the breaker state may not exist when reset_session_state was not
    # called on this worker path — without it the ``+= 1`` below raises and aborts
    # the turn, leaving the worker stuck running (observed in the soak).
    if not hasattr(agent, "_kanban_no_progress_streak"):
        agent._kanban_no_progress_streak = 0
        agent._kanban_no_progress_nudged = False
        agent._kanban_made_progress = False

    progress = _classify_worker_turn_progress(assistant_message)
    if progress in ("progress", "terminal"):
        agent._kanban_no_progress_streak = 0
        agent._kanban_no_progress_nudged = False
        # A real business/MCP tool ran, so the turn produced genuine work and the
        # auto-complete net may deliver its result. Structural signal, not a text guess.
        if progress == "progress":
            agent._kanban_made_progress = True
            return _verdict("fallthrough")

        # A SUCCESSFUL terminal kanban tool ends the run. The model is told to stop
        # after one (KANBAN_GUIDANCE rule 5) but weak models keep acting, and a second
        # pass has created duplicate purchase orders (2026-08-21). Re-read the live
        # status instead of trusting the tool's own return: a call that FAILED leaves
        # the task 'running' and the worker must be allowed to retry.
        status = None
        try:
            from hermes_cli import kanban_db as _kb
            conn = _kb.connect()
            try:
                status = getattr(_kb.get_task(conn, kanban_task), "status", None)
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        except Exception:
            pass
        if status and status != "running":
            logger.info(
                "kanban worker %s: terminal kanban tool succeeded (status=%s) — ending run",
                kanban_task, status,
            )
            _turn_exit_reason = f"kanban_terminal_tool(status={status})"
            return _verdict("break")
        return _verdict("fallthrough")

    agent._kanban_no_progress_streak += 1
    streak = agent._kanban_no_progress_streak
    action = _kanban_no_progress_decision(streak, agent._kanban_no_progress_nudged)

    if action == "nudge":
        agent._kanban_no_progress_nudged = True
        logger.warning(
            "kanban worker %s: %d non-progress turns — injecting corrective nudge",
            kanban_task, streak,
        )
        # French: this is spoken to the model that answers a French-speaking customer.
        messages.append({
            "role": "user",
            "content": (
                "Tu commentes/narres sans agir. Appelle directement l'outil métier "
                "(ou kanban_complete / kanban_block) MAINTENANT — n'écris pas ton intention."
            ),
            "_kanban_no_progress_nudge": True,
        })
        agent._stream_needs_break = True
        agent._session_messages = messages
        return _verdict("continue")

    if action == "block":
        _turn_exit_reason = f"kanban_no_progress_breaker(streak={streak})"
        final_response = agent._force_kanban_block_no_progress(kanban_task, streak)
        messages.append({"role": "assistant", "content": final_response})
        # French: user-facing status shown in the desk chat.
        agent._emit_status("⛔ Worker en boucle (narration) — tâche bloquée pour intervention")
        return _verdict("break")

    return _verdict("fallthrough")
