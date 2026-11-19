# //// Neoffice — added file (no upstream equivalent): a request that cannot fit in the window is never sent
# //// (neoffice-maintenance#3118)
"""A request whose pressure reaches the context window is a proven overflow, never sent as it is (#3118).

A pole worker's compaction failed once (its summary came back cut), which armed the summary-failure cooldown; the
pre-API gate skipped compaction during that cooldown whatever the pressure, two large reads pushed the next request
past the window, and the self-hosted server answered it with an empty stream, read as a transient error. Now, on a
kanban worker's own turn only (a person's chat keeps upstream's gate, whose exhaustion would reset the session):
- the pre-API gate gives a request whose usage-anchored pressure cannot fit the one real attempt a provider-proven
  overflow gets (it bypasses the cooldown), bounded by the attempt budget, and fails closed when nothing compacts it;
  a rough estimate past the window is no proof and goes out as upstream sends it;
- an empty stream on a request that reaches the window is read as an overflow (upstream compacts and retries);
- a card whose turn ends on that exhausted compaction asks its person one question instead of crashing; a compaction
  that timed out is left as upstream books it.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent import conversation_loop as loop
from agent import turn_preflight as tp
from agent.error_classifier import FailoverReason, classify_api_error
from agent.errors import EmptyStreamError

WINDOW = 65_536
THRESHOLD = 55_705


# ── the pre-API gate ─────────────────────────────────────────────────────────────────────────

@pytest.fixture
def gate(monkeypatch):
    """The real gate, its compaction helpers faked: ``calls`` keeps every _compress_context call's kwargs."""
    for name, value in {
        "ensure_compression_feasibility_checked": lambda agent, tokens: None,
        "_review_fork_first_request_pending": lambda agent: False,
        "automatic_compaction_status_message": lambda *a, **k: None,
        "context_compression_timed_out": lambda agent: False,
        "compression_skipped_due_to_lock": lambda agent: False,
        "compression_blocked_transiently": lambda agent: False,
        "conversation_history_after_compression": lambda agent, messages, history: history,
        "_clear_overflow_warn": lambda agent: None,
        "_refund_api_call": lambda agent, count: count,
        "_reset_retry_state_after_compaction": lambda agent: None,
        "_blocked_compress_reason": lambda *a, **k: None,
    }.items():
        monkeypatch.setattr(tp, name, value)
    monkeypatch.setattr(loop, "_maybe_grow_local_window", lambda *a, **k: None)
    monkeypatch.setattr(loop, "_should_skip_model_call_for_reference_handoff", lambda messages, user: False)
    monkeypatch.setattr(loop, "_provider_overflow_exhausted_result", lambda *a, **k: {"failure_reason": "context_overflow"})
    calls = []

    def run(pressure, *, cooldown=42, attempts=0, enabled=True, card=True, anchored=True, v=None, unchanged=False):
        if card:
            monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test")
        else:
            monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)

        def compress(messages, system_message, **kwargs):
            calls.append(kwargs)
            return (messages if unchanged else list(messages)), system_message  # a new list: compacted

        agent = SimpleNamespace(
            compression_enabled=enabled, model="nora", _compress_context=compress, _turn_origin=None,
            _request_pressure_anchored=anchored,
            _emit_status=lambda *a: None, _persist_session=lambda *a: None,
            _warn_context_overflow_blocked=lambda *a: None, _warn_uncompressed_context_overflow=lambda *a: None,
        )
        compressor = SimpleNamespace(
            context_length=WINDOW, threshold_tokens=THRESHOLD,
            get_active_compression_failure_cooldown=lambda: cooldown,
            should_compress=lambda tokens: tokens >= THRESHOLD and not cooldown,
        )
        v = v or tp.PreflightGateVerdict(
            action="", pending_moa_prepared_request=None,
            messages=[{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}],
            active_system_prompt="sys", conversation_history=[], api_call_count=3, compression_attempts=attempts,
            final_response=None, failed=False, _turn_exit_reason=None, _compression_timeout_exhausted=False,
            _preflight_compression_blocked=False, _provider_overflow_recovery_pending=False,
            _last_preflight_pressure=None,
        )
        return tp.run_preflight_compression(
            agent, v, compressor=compressor, request_pressure_tokens=pressure, provider_overflow_preflight=False,
            defer_preflight=lambda tokens: False, moa_prepared_request=None, system_message="sys",
            user_message="q", max_compression_attempts=3, effective_task_id="t_test",
        )

    run.calls = calls
    return run


def test_a_request_over_the_window_is_compacted_through_the_cooldown(gate):
    verdict = gate(70_000)
    assert verdict.action == "continue", "compacted, then measured again: never sent as it was"
    assert [c.get("bypass_cooldown") for c in gate.calls] == [True]


def test_under_the_window_the_cooldown_still_defers_and_the_request_goes(gate):
    verdict = gate(60_000)
    assert verdict.action == "fallthrough" and gate.calls == [], "upstream's behaviour under the window"


def test_a_request_over_the_window_with_no_attempt_left_fails_closed(gate):
    verdict = gate(70_000, attempts=3)
    assert verdict.action == "return" and verdict.result == {"failure_reason": "context_overflow"}
    assert gate.calls == []


def test_with_compaction_disabled_upstream_warns_and_sends(gate):
    verdict = gate(70_000, enabled=False)
    assert verdict.action == "fallthrough" and gate.calls == []


@pytest.mark.parametrize("attempts", [0, 3])
def test_outside_a_card_the_gate_is_upstream_s(gate, attempts):
    # A person's chat: upstream sends during the cooldown, and its exhaustion would reset the session.
    verdict = gate(70_000, card=False, attempts=attempts)
    assert verdict.action == "fallthrough" and gate.calls == []


def test_a_rough_estimate_past_the_window_is_no_proof(gate):
    verdict = gate(70_000, anchored=False)
    assert verdict.action == "fallthrough" and gate.calls == [], "upstream: estimate magnitude is not evidence"


def test_three_attempts_then_the_card_s_request_fails_closed(gate):
    first = gate(70_000)
    for n in (2, 3):
        assert gate(70_000, v=first).action == "continue", n
    assert first.compression_attempts == 3
    last = gate(70_000, v=first)
    assert last.action == "return" and last.result == {"failure_reason": "context_overflow"}
    assert [c.get("bypass_cooldown") for c in gate.calls] == [True, True, True]


def test_a_compaction_deferred_under_a_lock_still_never_sends(gate, monkeypatch):
    monkeypatch.setattr(tp, "compression_skipped_due_to_lock", lambda agent: True)
    verdict = gate(70_000, unchanged=True)
    assert verdict.action == "return" and verdict.compression_attempts == 0, "the attempt refunded, nothing sent"


# ── an empty stream ──────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("tokens, reason", [
    (70_000, FailoverReason.context_overflow),
    (WINDOW, FailoverReason.context_overflow),
    (59_720, FailoverReason.server_error),
    (2_000, FailoverReason.server_error),
])
def test_an_empty_stream_over_the_window_is_an_overflow(tokens, reason, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test")
    error = EmptyStreamError("Provider returned an empty stream with no finish_reason")
    verdict = classify_api_error(error, provider="custom", model="nora", approx_tokens=tokens, context_length=WINDOW)
    assert verdict.reason is reason
    assert verdict.should_compress is (reason is FailoverReason.context_overflow)


def test_outside_a_card_an_empty_stream_stays_transient(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    error = EmptyStreamError("Provider returned an empty stream with no finish_reason")
    verdict = classify_api_error(error, provider="custom", model="nora", approx_tokens=70_000, context_length=WINDOW)
    assert verdict.reason is FailoverReason.server_error


def test_a_card_s_empty_stream_over_the_window_is_compacted_then_answered(monkeypatch):
    # The real agent loop, its provider faked: an empty stream on a request over the window, then an answer.
    from unittest.mock import patch

    from tests.agent.test_tool_call_guardrail_runtime import _make_agent, _mock_response

    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test")
    monkeypatch.setenv("HERMES_KANBAN_STOP_NUDGE", "0")  # the answer ends the turn: the board nudge is not tested here
    agent = _make_agent("web_search", max_iterations=5)
    agent._disable_streaming = True
    agent.compression_enabled = True
    agent.context_compressor.context_length = 50
    question = "a question about the ledger " * 40  # its request is over the window, whatever is counted
    compacted = []

    def compress(messages, system_message, **kwargs):
        compacted.append(agent.client.chat.completions.create.call_count)  # model calls made before it
        return [{"role": "user", "content": "the ledger question"}], system_message  # a compaction shrinks

    monkeypatch.setattr(agent, "_compress_context", compress)
    agent.client.chat.completions.create.side_effect = [
        EmptyStreamError("Provider returned an empty stream with no finish_reason"),
        _mock_response(content="the answer"),
    ]
    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation(question)
    assert result["final_response"] == "the answer"
    assert compacted == [1], "upstream's overflow path compacted once, after the empty stream, before the retry"
    assert agent.client.chat.completions.create.call_count == 2


def test_an_empty_stream_is_judged_against_the_requests_pressure_and_only_it():
    from agent.turn_api_error import _neoffice_classification_tokens

    agent = SimpleNamespace(_neoffice_request_pressure=70_800)
    empty = EmptyStreamError("Provider returned an empty stream with no finish_reason")
    assert _neoffice_classification_tokens(agent, empty, 63_000) == 70_800
    assert _neoffice_classification_tokens(agent, RuntimeError("server disconnected"), 63_000) == 63_000
    assert _neoffice_classification_tokens(SimpleNamespace(), empty, 63_000) == 63_000


# ── the card asks its person ─────────────────────────────────────────────────────────────────

def test_a_card_ending_on_an_overflow_asks_one_question(monkeypatch):
    from agent import kanban_turn_recovery as ktr
    from neoffice_branding import reply
    from tools import kanban_tools

    import agent.i18n

    monkeypatch.setattr(agent.i18n, "get_language", lambda: "fr")
    seen = []
    monkeypatch.setattr(kanban_tools, "_handle_block",
                        lambda args, **kw: seen.append(args) or json.dumps({"ok": True, "status": "blocked"}))
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test")
    blocked = ktr._neoffice_block_truncated_turn(
        {"failed": True, "failure_reason": "context_overflow", "turn_exit_reason": "context_compression_exhausted"},
        task_id="t_test", check=lambda: True)
    assert blocked is True
    assert [(a["reason"], a["kind"]) for a in seen] == [(reply("overflow_card", "fr"), "needs_input")]
    # A compaction that timed out carries the same failure_reason but says nothing of the request's size.
    timed_out = ktr._neoffice_block_truncated_turn(
        {"failed": True, "failure_reason": "context_overflow", "turn_exit_reason": "context_compression_timeout"},
        task_id="t_test", check=lambda: True)
    assert timed_out is False and len(seen) == 1


@pytest.mark.parametrize("key", ["overflow_card", "truncated_card"])
@pytest.mark.parametrize("lang", ["fr", "de", "it", "en"])
def test_a_card_question_is_one_question_with_no_colon(key, lang):
    from neoffice_branding import reply

    text = reply(key, lang)
    assert text.endswith("?") and text.count("?") == 1, "the accounting chat keeps the questions of a block reason"
    assert ":" not in text, "the chat line already reads « ✋ <pole> : <question> »"
