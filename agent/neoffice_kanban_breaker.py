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
    # A fork turn (the post-task skill review, a side question) runs in the worker's process
    # and inherits HERMES_KANBAN_TASK, but it is not the worker: on 2026-09-25 the breaker cut
    # the first skill review that ever started, after three refused skill_manage calls it could
    # have corrected (a 173-char description, then a YAML quote).
    if getattr(agent, "_turn_origin", None):
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
            from hermes_cli import kanban_db_connect as _kbc  # kanban_db.connect: compat pointer only
            conn = _kbc.connect()
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


# //// Neoffice — a worker stopped by the tool-loop guardrail answers with what it found.
# //// 2026-09-25: a compta worker asked for a five-part treasury review read the bank
# //// balances, the receivables by age, the payables and the reminders due, then called the
# //// same empty listing again and again. The guardrail stopped it, rightly, and its canned
# //// stop message reached the person as « je n'ai pas réussi » with four answers of five in
# //// hand. A kanban worker's final text is what the person reads (the auto-complete net
# //// completes the card with it), so the model is asked once more for what it found and what
# //// it could not get: the summary call upstream makes at the iteration limit
# //// (chat_completion_helpers.handle_max_iterations). On any failure the canned message stands.
# ////
# //// The answer is read where a worker puts it. Upstream reads the text content only and
# //// discards tool calls; our workers are told by their system prompt to end with
# //// kanban_complete or kanban_block, and they do even when asked for plain text. Measured on
# //// a ventes stop (2026-09-25, 10:13): with the tools declared the model answered 3/3 with
# //// kanban_block(reason="…the right question for the user…"), content empty; without the
# //// tools it wrote the same call as text and the vLLM stack returned content=null, 3/3, so
# //// the summary came back empty and the person heard the generic failure. The call keeps the
# //// tools (the cached prefix: ~2 s instead of a 14 s re-prefill) and the summary of a
# //// kanban_complete or the reason of a kanban_block is the answer, also inside the
# //// tool_search bridge (tool_call with calls=[...]).
NEOFFICE_GUARDRAIL_SUMMARY_REQUEST = (
    "The system stopped the tool {tool}: it kept returning the same result. Do not call it "
    "again, nor any other tool that looks something up. Finish now: call kanban_complete with "
    "your answer to the user as the summary, in their language, from what you have ALREADY "
    "found, saying plainly which part of the request you could not get. If you need something "
    "from the user to go on, call kanban_block with your question as the reason instead."
)
NEOFFICE_TERMINAL_TEXT_KEYS = {"kanban_complete": ("summary", "result"), "kanban_block": ("reason",)}


def _neoffice_json_args(raw: Any) -> dict:
    import json

    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw) if isinstance(raw, str) and raw.strip() else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def neoffice_terminal_call_text(tool_calls: Any) -> str:
    """The user-facing text of a kanban_complete / kanban_block call, "" when there is none.

    A call may come wrapped in the tool_search bridge: tool_call with calls=[{name, arguments}]."""
    calls = []
    for tc in tool_calls or ():
        fn = getattr(tc, "function", None) or tc
        name = str(getattr(fn, "name", "") or "")
        args = _neoffice_json_args(getattr(fn, "arguments", None))
        if name == "tool_call":
            for inner in args.get("calls") or ():
                if isinstance(inner, dict):
                    calls.append((str(inner.get("name") or ""), _neoffice_json_args(inner.get("arguments"))))
        else:
            calls.append((name, args))
    for name, args in calls:
        for key in NEOFFICE_TERMINAL_TEXT_KEYS.get(name, ()):
            value = args.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _neoffice_summary_attempt(agent: Any, api_messages: list, request_id: str, seen: list):
    """Upstream's chat-completions summary call (tools kept), read for a terminal kanban call.

    ``seen`` collects what each empty attempt returned, for the warning."""
    import re

    from agent import chat_completion_helpers as _cch

    kwargs = agent._build_api_kwargs(api_messages)
    _cch.sanitize_outbound_kwargs(agent, kwargs)

    def _attempt(retry_count: int) -> str:
        client = agent._ensure_primary_openai_client(reason="neoffice_guardrail_summary")
        response = _cch._managed_summary_call(
            agent, request_id, kwargs,
            lambda request: client.chat.completions.create(**_cch.bypass_chat_sdk_request_transform(request, client)),
            retry_count=retry_count,
        )
        if _cch.is_router_timeout_shim(response):
            seen.append("router timeout shim")
            return ""
        normalized = agent._get_transport().normalize_response(response)
        text = neoffice_terminal_call_text(normalized.tool_calls)
        if not text:
            text = re.sub(r"<think>.*?</think>\s*", "", normalized.content or "", flags=re.DOTALL).strip()
        if not text:
            names = [str(getattr(getattr(tc, "function", None) or tc, "name", "") or "?")
                     for tc in (normalized.tool_calls or ())]
            seen.append(f"tool calls {names}" if names else f"no text (finish={normalized.finish_reason})")
        return text

    return _attempt


def neoffice_guardrail_summary(agent: Any, messages: list, decision: Any) -> str:
    """The worker's answer from what it found after a guardrail stop.

    "" outside a kanban worker, or when the summary call fails or comes back empty."""
    task_id = (os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    if not task_id or getattr(agent, "_turn_origin", None):  # a fork turn's text reaches nobody
        return ""
    import uuid
    from contextlib import suppress

    from agent import chat_completion_helpers as _cch
    from agent import relay_llm
    from agent.message_metadata import append_message

    request_id = f"neoffice-guardrail-summary:{uuid.uuid4()}"
    outcome = "failed"
    tool = getattr(decision, "tool_name", "") or "a tool"
    seen: list = []
    append_message(messages, {"role": "user", "content": NEOFFICE_GUARDRAIL_SUMMARY_REQUEST.format(tool=tool)})
    try:
        api_messages = _cch._iteration_summary_api_messages(agent, messages)
        build = _cch._SUMMARY_ATTEMPT_BUILDERS.get(agent.api_mode)  # other transports: upstream's text read
        attempt = build(agent, api_messages, request_id) if build else _neoffice_summary_attempt(
            agent, api_messages, request_id, seen)
        for retry_count in (0, 1):
            text = (attempt(retry_count) or "").strip()
            if text:
                outcome = "success"
                logger.info("kanban worker %s: guardrail stop on %s answered with what it found (%d chars)",
                            task_id, tool, len(text))
                return text
        logger.warning("kanban worker %s: guardrail summary on %s came back empty (%s)",
                       task_id, tool, "; ".join(seen) or "empty text")
    except Exception:  # noqa: BLE001 — the canned stop message still ends the turn
        logger.warning("kanban worker %s: guardrail summary failed", task_id, exc_info=True)
    finally:
        with suppress(Exception):
            relay_llm.complete_logical_call(request_id, outcome=outcome)
    return ""
# //// END Neoffice ////


# //// Neoffice — a worker somebody is waiting on asks the inference proxy to serve it first.
# //// The Olares proxy honours an explicit `priority` and gives 10 to any prompt over 16 KB
# //// otherwise, so a worker's ~70 KB turn queued behind the skill reviews and the nightly memory
# //// pass. Measured on the engine over 33 h (2026-09-25): a first agent call is slow in 18-20 %
# //// of cases when nothing waits, 45-74 % when requests queue. A kanban worker whose card has a
# //// notify subscriber (a chat, a voice call, WhatsApp) sends priority 0; fork turns, cron and
# //// background tasks keep the proxy's default. Only toward our own proxy: another provider
# //// would reject the unknown field.
NEOFFICE_INTERACTIVE_PRIORITY = 0
NEOFFICE_PRIORITY_HOSTS = ("noraai.ch",)
_NEOFFICE_SUBSCRIBED: dict = {}


def _neoffice_task_has_subscriber(task_id: str) -> bool:
    """Whether a chat waits on this card; read once per task (a worker process serves one)."""
    if task_id not in _NEOFFICE_SUBSCRIBED:
        answer = False
        try:
            from hermes_cli import kanban_db_connect as _kbc
            from hermes_cli import kanban_db_notify as _kbn

            conn = _kbc.connect(board=os.environ.get("HERMES_KANBAN_BOARD") or None)
            try:
                answer = bool(_kbn.list_notify_subs(conn, task_id))
            finally:
                conn.close()
        except Exception:  # noqa: BLE001 — no priority is the safe answer
            answer = False
        _NEOFFICE_SUBSCRIBED[task_id] = answer
    return _NEOFFICE_SUBSCRIBED[task_id]


def neoffice_request_priority(agent: Any, kwargs: dict) -> dict:
    """`kwargs` with extra_body.priority = 0 when a person waits on this worker's answer."""
    task_id = (os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    if not task_id or getattr(agent, "_turn_origin", None):
        return kwargs
    if getattr(agent, "api_mode", None) != "chat_completions":
        return kwargs
    base_url = str(getattr(agent, "base_url", "") or "")
    if not any(host in base_url for host in NEOFFICE_PRIORITY_HOSTS):
        return kwargs
    if not _neoffice_task_has_subscriber(task_id):
        return kwargs
    extra = kwargs.get("extra_body")
    extra = dict(extra) if isinstance(extra, dict) else {}
    extra.setdefault("priority", NEOFFICE_INTERACTIVE_PRIORITY)
    kwargs["extra_body"] = extra
    return kwargs
# //// END Neoffice ////
