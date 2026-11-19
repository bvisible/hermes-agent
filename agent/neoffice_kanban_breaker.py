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
import re
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

logger = logging.getLogger(__name__)


# //// Neoffice — the hesitation budget (27.09). A worker whose pole has no tool for the request
# //// does not stop: it rebuilds the answer by hand. « Qui paie en retard ? » reached rh by a routing
# //// slip, and its worker listed and counted invoices for two minutes, twenty model calls, until
# //// the repetition guard cut it; the person waited with nothing on screen. What a secretary does
# //// instead is ask. After NEOFFICE_KANBAN_HESITATION_ROUNDS tool rounds (8 by default, 0 turns it
# //// off) the worker is told once to finish or to ask the user one short question, and four rounds
# //// later to finish now. On the night of 26 to 27.09, 63 of the capability bench's 70 worker
# //// sessions took 6 calls or fewer; the 7 above were loops and wrong routes (a credit note in 25
# //// calls, a refusal in 20) and a purchase order in 12.
NEOFFICE_HESITATION_NUDGE = (
    "You have made {n} tool calls and not answered yet. If your tools cannot answer this request "
    "directly, or if it can be read two ways, stop now: call kanban_block with ONE short question for "
    "the user, or kanban_complete saying plainly what you cannot do here. Do not rebuild the answer by "
    "hand from raw listings. If you already have what you need, finish with kanban_complete."
)
NEOFFICE_HESITATION_FINISH = (
    "{n} tool calls: finish now. Call kanban_complete with your answer from what you ALREADY have, in "
    "the user's language, saying which part you could not get, or kanban_block with your question if "
    "you need the user. Do not call any other tool."
)


def neoffice_hesitation_nudge(rounds: int, first: int | None = None) -> str:
    """The message a kanban worker gets after its `rounds`-th tool round, "" when none is due."""
    if first is None:
        try:
            first = int(os.environ.get("NEOFFICE_KANBAN_HESITATION_ROUNDS", "8") or 0)
        except ValueError:
            first = 8
    if first <= 0:
        return ""
    if rounds == first:
        return NEOFFICE_HESITATION_NUDGE.format(n=rounds)
    if rounds == first + 4:
        return NEOFFICE_HESITATION_FINISH.format(n=rounds)
    return ""
# //// END Neoffice ////


# //// Neoffice — the summary a terminal kanban call carries (kanban_complete's summary, kanban_block's reason).
def _neoffice_terminal_summary(assistant_message: Any) -> str:
    """The text a terminal kanban tool call hands over, '' when it carries none."""
    import json as _json

    for call in getattr(assistant_message, "tool_calls", None) or []:
        function = getattr(call, "function", None)
        if getattr(function, "name", "") not in ("kanban_complete", "kanban_block"):
            continue
        try:
            args = _json.loads(getattr(function, "arguments", "") or "{}")
        except (TypeError, ValueError):
            continue
        if isinstance(args, dict):
            for key in ("summary", "result", "reason"):
                value = args.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return ""
# //// END Neoffice ////


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
    # //// Neoffice — every tool round counts toward the hesitation budget (see above).
    agent._kanban_tool_rounds = getattr(agent, "_kanban_tool_rounds", 0) + 1

    progress = _classify_worker_turn_progress(assistant_message)
    if progress in ("progress", "terminal"):
        agent._kanban_no_progress_streak = 0
        agent._kanban_no_progress_nudged = False
        # A real business/MCP tool ran, so the turn produced genuine work and the
        # auto-complete net may deliver its result. Structural signal, not a text guess.
        if progress == "progress":
            # //// Neoffice — a read that declares its final answer closes the card (neoffice_close_on_final_read).
            first_business_round = not getattr(agent, "_kanban_made_progress", False)
            # //// END Neoffice ////
            agent._kanban_made_progress = True
            # //// Neoffice — no model turn to rewrite what the task's first and only read already answered.
            if first_business_round:
                answer = neoffice_close_on_final_read(agent, kanban_task, assistant_message, messages)
                if answer:
                    final_response = answer
                    _turn_exit_reason = "neoffice_final_read"
                    messages.append({"role": "assistant", "content": answer})
                    return _verdict("break")
            # //// END Neoffice ////
            # //// Neoffice — the hesitation budget: told once to finish or ask, then to finish.
            nudge = neoffice_hesitation_nudge(agent._kanban_tool_rounds)
            if nudge:
                logger.warning(
                    "kanban worker %s: %d tool rounds without an answer — hesitation nudge",
                    kanban_task, agent._kanban_tool_rounds,
                )
                messages.append({"role": "user", "content": nudge, "_kanban_hesitation_nudge": True})
                agent._stream_needs_break = True
                agent._session_messages = messages
                return _verdict("continue")
            # //// END Neoffice ////
            return _verdict("fallthrough")

        # //// Neoffice — a goal-mode card keeps upstream's continuation: its goal loop (hermes_cli.goals,
        # //// run by gateway/session_kanban.py) consults the model again after the card is completed, by
        # //// design. NORA creates no goal-mode card; the stop below is for the cards it does create.
        if os.environ.get("HERMES_KANBAN_GOAL_MODE") == "1":
            return _verdict("fallthrough")
        # //// END Neoffice ////
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
            # //// Neoffice — the card's own answer is the turn's answer. Since the profile owner runs the card
            # //// (v0.22.3, gateway/session_kanban.py) the worker's exit code is read from this turn's result
            # //// (hermes_cli.turn_exit): a turn that ends on the tool call, with no text, counted as « not
            # //// completed » and the worker exited 1 although its card was done (upstream's owner probe).
            if not final_response:
                final_response = _neoffice_terminal_summary(assistant_message) or f"kanban task {status}"
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


# //// Neoffice — the last answer asked once more WITHOUT tools (neoffice-maintenance#3016). With the tools declared,
# //// a worker asked for its answer at a guardrail stop sometimes called another tool instead (frappe_calc 10 times,
# //// frappe_entry_check 2, wiki_search 1 on the accounting exam of 18.11): the call is neither run nor read, the
# //// answer came back empty twice and the card ended done on the canned stop text. With the profile key
# //// tool_loop_guardrails.neoffice_final_answer_without_tools, the second attempt sends tool_choice "none": the
# //// tools stay declared, so the cached prefix holds (the 25.09 measure: ~2 s instead of a 14 s re-prefill), but no
# //// call can be made. The model may then write a call as TEXT (on 25.09, without tools, it wrote its kanban_block
# //// that way): a written kanban_complete or kanban_block is read for its answer. A written lookup says, by its
# //// structure alone, that the model meant to go on: that is no answer, even after a sentence that reads like one,
# //// so the card is blocked and the text the model wrote around the call (without its markup) is the block's
# //// reason, delivered to the person: nothing is lost, and nothing reads as done when it is not.
def neoffice_final_answer_without_tools(agent: Any) -> bool:
    """Whether the profile turned the tool-less second attempt on (off when the agent has no guardrail config)."""
    config = getattr(getattr(agent, "_tool_guardrails", None), "config", None)
    return bool(getattr(config, "neoffice_final_answer_without_tools", False))


_WRITTEN_FUNCTION = re.compile(r"<function=([^>\s]+)>(.*?)(?:</function>|\Z)", re.DOTALL)
_WRITTEN_PARAMETER = re.compile(r"<parameter=([^>\s]+)>\s*(.*?)\s*</parameter>", re.DOTALL)
_WRITTEN_JSON_CALL = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)
_WRITTEN_MARKUP = re.compile(r"<tool_call>.*?(?:</tool_call>|\Z)|<function=[^>\s]+>.*?(?:</function>|\Z)", re.DOTALL)
_FENCE = re.compile(r"```(?:[^\n`]*\n)?(.*?)```", re.DOTALL)  # one code block at a time, its info line optional


def neoffice_written_call_text(content: Any) -> str | None:
    """The answer inside calls the model wrote as text (qwen3_coder XML or a JSON <tool_call>).

    None when the text holds no written call; "" when its written calls hold no kanban_complete or kanban_block
    answer: the model meant to go on, which is no answer (neoffice_written_lookup_text keeps what it wrote around
    them, for the block)."""
    text = content if isinstance(content, str) else ""
    if "<function=" not in text and "<tool_call>" not in text:
        return None
    calls = [SimpleNamespace(name=name, arguments=dict(_WRITTEN_PARAMETER.findall(body)))
             for name, body in _WRITTEN_FUNCTION.findall(text)]
    for raw in _WRITTEN_JSON_CALL.findall(text):
        value = _neoffice_json_args(raw)
        if value.get("name"):
            calls.append(SimpleNamespace(name=str(value["name"]), arguments=value.get("arguments") or {}))
    return neoffice_terminal_call_text(calls)


def neoffice_written_lookup_text(content: Any) -> str:
    """What the model wrote around calls written as text, without their markup and without a code fence left empty:
    the reason a card stopped on written lookups is blocked with, read by the person. An unclosed call runs to the
    end of the text (the model stops in it)."""
    text = re.sub(r"<think>.*?</think>\s*", "", content if isinstance(content, str) else "", flags=re.DOTALL)
    return _FENCE.sub(lambda block: block.group(0) if block.group(1).strip() else "",
                      _WRITTEN_MARKUP.sub("", text)).strip()


def neoffice_unanswered_text(agent: Any) -> str:
    """The text a worker wrote around the lookups it wrote instead of answering, at the last guardrail summary."""
    return getattr(agent, "_neoffice_unanswered_text", "") or ""


def neoffice_worker_without_tools(agent: Any) -> bool:
    """The key is on and this is a kanban worker's own turn (not a fork turn such as the skill review)."""
    return (bool((os.environ.get("HERMES_KANBAN_TASK") or "").strip()) and not getattr(agent, "_turn_origin", None)
            and neoffice_final_answer_without_tools(agent))


def neoffice_worker_answer_from_text(agent: Any, content: Any) -> str | None:
    """For the iteration-limit summary: the answer of a call a worker wrote as text; for written lookups, the text
    written around them without their markup ("" when there is none: no answer, upstream's retry and stop text
    follow); None when the key is off, outside a worker, or when the text holds no written call (upstream's read then
    stands).

    Nothing is blocked on this path: with the key off upstream sends the content as written, so a real answer
    followed by a written lookup reaches the person; here it does too, without the markup."""
    if not neoffice_worker_without_tools(agent):
        return None
    text = re.sub(r"<think>.*?</think>\s*", "", content if isinstance(content, str) else "", flags=re.DOTALL).strip()
    answer = neoffice_written_call_text(text)
    return neoffice_written_lookup_text(text) if answer == "" else answer


def neoffice_block_unanswered(agent: Any, text: str) -> bool:
    """With the key on, a worker the guardrail stopped with no answer blocks its card on the stop text.

    Left alone, the auto-complete net (turn_finalizer) completed the card done on that canned text: 13 cards of the
    accounting exam read as answered. Blocked, the card says it was not answered and the person reads the same text.
    False outside a kanban worker, in a fork turn, with the key off, or when the block is refused."""
    task_id = (os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    reason = (text or "").strip()
    if not reason or not neoffice_worker_without_tools(agent):
        return False
    try:
        import json

        from tools.kanban_tools import _handle_block

        outcome = _handle_block({"reason": reason[:4000]})
        error = (json.loads(outcome) or {}).get("error") if isinstance(outcome, str) else None
    except Exception:  # the turn still ends; the net then completes the card as before
        logger.warning("kanban worker %s: blocking the unanswered card failed", task_id, exc_info=True)
        return False
    if error:
        logger.warning("kanban worker %s: blocking the unanswered card was refused: %s", task_id, str(error)[:300])
        return False
    logger.info("kanban worker %s: stopped without an answer, the card is blocked, not done", task_id)
    return True
# //// END Neoffice ////


# //// Neoffice — a read that declares its final answer closes the card, without a model turn (#3081).
# //// A one-call read of nora (a ranking, a total) already answers the question; the worker's next model
# //// turn only rewrote it into kanban_complete: 2.5 to 4 s more per question through a pole. The contract: the
# //// tool puts `final_answer`, a ready sentence of the facts it just read (what was read, for whom, over which
# //// period) in the person's language, at the top of its result, only from its own code and only when its read is
# //// complete. Two marks are needed, neither a guess of the code: the profile names the tools allowed to close a card
# //// by their full MCP name (`kanban.neoffice_final_read_tools`, a list, absent by default: nothing changes), and the
# //// router, which read the whole request, marked the task as one read when it created it (a task event
# //// `neoffice_answer_shape` {"shape": "one_read"} with no run). A request that asks two things is not marked, so
# //// its first read never closes it. The card then closes only when the task's first business round is a single
# //// call to a named tool whose result carries the field, through kanban_complete's own handler and guards; any
# //// refusal leaves the usual model turn. Such a card skips the worker's memory and skill review
# //// (turn_finalizer._neoffice_worker_task_done reads only kanban_complete and text endings), on purpose: a read
# //// that states its own answer teaches the pole nothing, and the review would cost a model call.
NEOFFICE_FINAL_ANSWER_MAX = 1500
NEOFFICE_ANSWER_SHAPE_EVENT = "neoffice_answer_shape"


def neoffice_final_read_tools(config: Any = None) -> frozenset:
    """The full MCP tool names the profile lets close a card with their own answer; empty when the key is absent or is
    not a list (a single name given as a string is refused, like any other malformed value). A name without the
    `mcp__` prefix is dropped: it names no MCP tool, but would name a built-in tool of that name."""
    try:
        if config is None:
            from hermes_cli.config import load_config

            config = load_config()
        names = ((config or {}).get("kanban") or {}).get("neoffice_final_read_tools") or []
    except Exception:
        return frozenset()
    if not isinstance(names, (list, tuple)):
        return frozenset()
    return frozenset(name.strip() for name in names if isinstance(name, str) and name.strip().startswith("mcp__"))


def neoffice_final_read_answer(assistant_message: Any, messages: Any, names: frozenset) -> str:
    """The `final_answer` of the round's single call when the profile names its tool, '' otherwise.

    Only the exact full name counts (`mcp__<server>__<tool>`): a tool of the same name on another server never
    closes a card. The tool message holds the MCP client's rendering, {"result": "<the server's text>"}, whose text
    is the JSON nora returned, or the server's {"_truncated": ...} envelope when the result was cut: a cut result never
    answers."""
    import json

    calls = getattr(assistant_message, "tool_calls", None) or []
    if not names or len(calls) != 1:
        return ""
    call = calls[0]
    name = getattr(getattr(call, "function", None), "name", "") or ""
    if name not in names:
        return ""
    call_id = getattr(call, "id", None)
    content = next(
        (m.get("content") for m in reversed(messages or [])
         if isinstance(m, dict) and m.get("role") == "tool" and call_id and m.get("tool_call_id") == call_id),
        None,
    )
    if not isinstance(content, str):
        return ""
    try:
        outer = json.loads(content)
        read = outer.get("result") if isinstance(outer, dict) else None
        if isinstance(read, str):
            read = json.loads(read)
    except (TypeError, ValueError):
        return ""
    if not isinstance(read, dict) or read.get("_truncated") or read.get("success") is False or read.get("error"):
        return ""
    answer = read.get("final_answer")
    answer = answer.strip() if isinstance(answer, str) else ""
    return answer if 0 < len(answer) <= NEOFFICE_FINAL_ANSWER_MAX else ""


def neoffice_task_reads_once(task_id: str) -> bool:
    """Whether the task was marked as one read when it was created: its first `neoffice_answer_shape` event with no
    run says {"shape": "one_read"}. False when there is none or it cannot be read, and once a run of the task ended
    completed or blocked: a task reopened, or unblocked by the person's answer, carries an ask the router did not
    read (a re-run after a crash or a timeout keeps the mark, its request is the same)."""
    import json

    try:
        from hermes_cli import kanban_db_connect as _kbc

        conn = _kbc.connect()
        try:
            row = conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = ? AND run_id IS NULL "
                               "AND NOT EXISTS (SELECT 1 FROM task_runs WHERE task_id = ? "
                               "AND outcome IN ('completed', 'blocked')) ORDER BY id LIMIT 1",
                               (task_id, NEOFFICE_ANSWER_SHAPE_EVENT, task_id)).fetchone()
        finally:
            conn.close()
        payload = json.loads(row[0]) if row and row[0] else {}
    except Exception:
        return False
    return isinstance(payload, dict) and payload.get("shape") == "one_read"


def _neoffice_card_status(task_id: str) -> Any:
    """The card's live status, None when it cannot be read."""
    try:
        from hermes_cli import kanban_db as _kb
        from hermes_cli import kanban_db_connect as _kbc

        conn = _kbc.connect()
        try:
            return getattr(_kb.get_task(conn, task_id), "status", None)
        finally:
            conn.close()
    except Exception:
        return None


def neoffice_close_on_final_read(agent: Any, task_id: str, assistant_message: Any, messages: Any) -> str:
    """Close the worker's card on its single read's final answer: that answer when the card closed, '' otherwise.

    The card closes through kanban_complete's own handler, so its guards (a preview handed off as done, a document
    name no tool returned, the accounting checks) still decide. A goal-mode card keeps upstream's continuation."""
    if os.environ.get("HERMES_KANBAN_GOAL_MODE") == "1":
        return ""
    answer = neoffice_final_read_answer(assistant_message, messages, neoffice_final_read_tools())
    if not answer or not neoffice_task_reads_once(task_id):
        return ""
    try:
        import json

        from tools.kanban_tools import _handle_complete

        outcome = _handle_complete({"summary": answer}, session_id=getattr(agent, "session_id", None))
        error = (json.loads(outcome) or {}).get("error") if isinstance(outcome, str) else None
    except Exception:  # the usual model turn follows
        logger.warning("kanban worker %s: closing the card on the read's own answer failed", task_id, exc_info=True)
        return ""
    if error:
        logger.warning("kanban worker %s: closing the card on the read's own answer was refused: %s",
                       task_id, str(error)[:300])
        return ""
    if _neoffice_card_status(task_id) == "running":
        return ""
    logger.info("kanban worker %s: its read answered by itself, the card is closed without a model turn", task_id)
    return answer
# //// END Neoffice ////


def neoffice_worker_answer_from_calls(agent: Any, tool_calls: Any) -> str:
    """A kanban worker's answer written as its terminal call, for upstream's summary read.

    Upstream's iteration-limit summary reads the text content only and discards tool calls;
    our workers answer it with kanban_complete / kanban_block, so 19 cards on osiris
    (2026-09-19..24: 16 compta, 2 support, 1 rh) ended on "I reached the iteration limit and
    couldn't generate a summary." with the answer written and thrown away. "" outside a
    kanban worker and in a fork turn (the skill review), where upstream's read stands."""
    if not tool_calls or not (os.environ.get("HERMES_KANBAN_TASK") or "").strip():
        return ""
    if getattr(agent, "_turn_origin", None):
        return ""
    return neoffice_terminal_call_text(tool_calls)


def _neoffice_summary_attempt(agent: Any, api_messages: list, request_id: str, seen: list, without_tools: bool = False):
    """Upstream's chat-completions summary call (tools kept), read for a terminal kanban call.

    ``seen`` collects what each empty attempt returned, for the warning. ``without_tools`` sends tool_choice "none"
    and reads a call written as text (see neoffice_final_answer_without_tools)."""
    from agent import chat_completion_helpers as _cch

    kwargs = agent._build_api_kwargs(api_messages)
    _cch.sanitize_outbound_kwargs(agent, kwargs)
    if without_tools:
        kwargs = dict(kwargs, tool_choice="none")

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
        written = neoffice_written_call_text(text) if without_tools and text else None
        if written is not None:
            if not written:
                seen.append("a lookup written as text")
                agent._neoffice_unanswered_text = neoffice_written_lookup_text(text)
                return ""
            text = written
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
    agent._neoffice_unanswered_text = ""  # set by an attempt that comes back as written lookups
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
        attempts = [attempt, attempt]
        if not build and neoffice_final_answer_without_tools(agent):  # see neoffice_final_answer_without_tools
            attempts[1] = _neoffice_summary_attempt(agent, api_messages, request_id, seen, without_tools=True)
        for retry_count, current in enumerate(attempts):
            text = (current(retry_count) or "").strip()
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
# //// notify subscriber (a chat, a voice call, WhatsApp) sends priority 0; an allowlisted voice
# //// source persisted by the NORA router sends -10, as the direct voice caller does. Fork turns, cron and
# //// background tasks keep the proxy's default. Only toward our own proxy: another provider
# //// would reject the unknown field.
NEOFFICE_INTERACTIVE_PRIORITY = 0
NEOFFICE_VOICE_PRIORITY = -10
NEOFFICE_VOICE_SOURCES = frozenset({"nora-console-voice", "nora-quick-voice", "nora-live-widget"})
NEOFFICE_PRIORITY_HOSTS = ("noraai.ch",)
_NEOFFICE_SUBSCRIBED: dict = {}
_NEOFFICE_TASK_PRIORITIES: dict = {}


def _neoffice_task_has_subscriber(task_id: str) -> bool:
    """Read subscriber status and allowed voice priority once per worker task."""
    if task_id not in _NEOFFICE_SUBSCRIBED:
        answer = False
        priority = NEOFFICE_INTERACTIVE_PRIORITY
        try:
            from hermes_cli import kanban_db_connect as _kbc
            from hermes_cli import kanban_db_notify as _kbn

            conn = _kbc.connect(board=os.environ.get("HERMES_KANBAN_BOARD") or None)
            try:
                subscribers = _kbn.list_notify_subs(conn, task_id)
                answer = bool(subscribers)
                for sub in subscribers:
                    metadata = sub.get("delivery_metadata")
                    if (sub.get("platform") == "webhook" and isinstance(metadata, dict)
                            and metadata.get("neoffice_request_source") in NEOFFICE_VOICE_SOURCES):
                        priority = NEOFFICE_VOICE_PRIORITY
                        break
            finally:
                conn.close()
        except Exception:  # noqa: BLE001 — no priority is the safe answer
            answer = False
        _NEOFFICE_SUBSCRIBED[task_id] = answer
        _NEOFFICE_TASK_PRIORITIES[task_id] = priority
    return _NEOFFICE_SUBSCRIBED[task_id]


def neoffice_request_priority(agent: Any, kwargs: dict) -> dict:
    """Subscribed voice workers use -10, other subscribers 0; background stays unchanged."""
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
    priority = _NEOFFICE_TASK_PRIORITIES.get(task_id, NEOFFICE_INTERACTIVE_PRIORITY)
    extra = kwargs.get("extra_body")
    extra = dict(extra) if isinstance(extra, dict) else {}
    extra.setdefault("priority", priority)
    kwargs["extra_body"] = extra
    return kwargs
# //// END Neoffice ////
