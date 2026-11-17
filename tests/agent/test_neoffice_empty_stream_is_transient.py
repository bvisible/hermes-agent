# //// Neoffice — added file (no upstream equivalent): an empty stream is a transient provider failure (#2810)
"""An empty stream from the provider is a transient server error that fails over at once (neoffice-maintenance#2810).

A provider stream that closes without a response raises EmptyStreamError (chat completions, Anthropic Messages,
Bedrock Converse, a tool_use block left incomplete). The stream reader already retried it on a fresh connection;
what reaches the classifier had no HTTP status and its words matched no pattern, so it was classified `unknown`: a
kanban worker then exited 1, a crash counted against its card, and the cards of a pole were abandoned at their
second empty stream. Matched by its type, it is now `server_error`, not retryable, fallback allowed (the verdict of
upstream's open fix): the turn moves to a fallback or ends at once, it never waits on the auto-recovery ladder nor
on the kanban in-place recovery, and a kanban worker exits on the transient code, so the card is requeued, spaced,
within the cap the dispatcher puts on such requeues (test_neoffice_transient_requeue_cap.py).
"""

from unittest.mock import patch

import pytest

from agent.error_classifier import FailoverReason, classify_api_error
from agent.errors import EmptyStreamError
from agent.kanban_turn_recovery import should_recover_turn
from agent.turn_api_error import settle_unrecovered_error
from agent.turn_retry_state import TurnRetryState
from hermes_cli.kanban_db import KANBAN_RATE_LIMIT_EXIT_CODE
from hermes_cli.turn_exit import turn_exit_code

EMPTY_STREAMS = [
    "Provider returned an empty stream with no finish_reason (possible upstream error or malformed SSE response).",
    "Provider returned an empty stream with no stop_reason (possible upstream error or malformed event stream).",
    "Provider returned an empty stream with no events (possible upstream error or malformed event stream).",
    "Anthropic Messages stream ended before message_stop (possible upstream stream drop).",
    "Bedrock Converse stream ended before messageStop; response is incomplete",
    "Stream ended with no stop_reason while a tool_use block was still incomplete; "
    "treating as a mid-tool-call stream drop (#80498).",
]


class _Busy(Exception):
    """A 503 as the self-hosted model sends it when it is busy."""

    status_code = 503


class _Agent:
    """The seams settle_unrecovered_error reads (as in test_turn_recovery_autorecover.py); the rest is a no-op."""

    log_prefix = ""
    verbose = False
    provider = "custom"
    platform = "cli"
    _credential_pool = None
    _current_streamed_assistant_text = ""
    _interrupt_requested = False

    def __init__(self, *, fallback_left=False):
        self.statuses, self.waits, self.activated = [], [], []
        self._fallback_left = fallback_left
        self._auto_recovery_cycles = 5

    def _has_pending_fallback(self):
        return self._fallback_left

    def _try_activate_fallback(self, **kwargs):
        self.activated.append(True)
        return self._fallback_left

    def _try_recover_primary_transport(self, *a, **k):
        return False

    def _emit_diagnostic_status(self, text):
        self.statuses.append(str(text))

    def _emit_diagnostic_wait(self, text):
        self.waits.append(str(text))

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def _settle(agent, err, *, retry_count):
    retry = TurnRetryState(primary_recovery_attempted=True)
    classified = classify_api_error(err, provider=agent.provider, model="nora")
    with patch("agent.conversation_loop._credential_refresh_profile", lambda a: None), \
            patch("agent.conversation_loop._arm_fallback_restart", lambda a, m, s, r: s), \
            patch("agent.turn_recovery.interruptible_backoff_sleep", lambda agent, *a, **k: None), \
            patch("agent.turn_api_error.max_retries_exhausted_result", lambda *a, **k: {"failed": True}), \
            patch("agent.turn_api_error.nonretryable_client_error_result",
                  lambda *a, **k: {"failed": True, "nonretryable": True}):
        verdict = settle_unrecovered_error(
            agent, api_error=err, classified=classified, _retry=retry, status_code=None,
            error_msg=str(err).lower(), is_context_length_error=False, is_rate_limited=False,
            _is_zai_coding_overload=False, _provider=agent.provider, _base="http://x", _model="nora",
            messages=[], api_messages=[], api_kwargs=None, active_system_prompt=None,
            conversation_history=None, approx_tokens=0, retry_count=retry_count, max_retries=3,
            compression_attempts=0, api_call_count=1,
        )
    return verdict, retry


@pytest.mark.parametrize("text", EMPTY_STREAMS)
def test_an_empty_stream_is_a_server_error_that_fails_over_and_is_not_retried(text):
    classified = classify_api_error(EmptyStreamError(text), provider="custom", model="nora")
    assert classified.reason is FailoverReason.server_error
    assert classified.retryable is False
    assert classified.should_fallback is True


def test_an_empty_stream_never_waits_on_the_ladder_it_ends_at_once():
    agent = _Agent()  # no fallback left
    verdict, retry = _settle(agent, EmptyStreamError(EMPTY_STREAMS[0]), retry_count=0)
    assert verdict.action == "return" and verdict.result.get("nonretryable")
    assert (retry.auto_recovery_cycles_used, agent.waits) == (0, [])


def test_an_empty_stream_moves_to_the_fallback_at_once():
    agent = _Agent(fallback_left=True)
    verdict, retry = _settle(agent, EmptyStreamError(EMPTY_STREAMS[0]), retry_count=0)
    assert (verdict.action, agent.activated, retry.auto_recovery_cycles_used) == ("break", [True], 0)


def test_a_kanban_worker_that_ends_on_an_empty_stream_exits_on_the_transient_code_and_is_not_rerun_in_place(
    monkeypatch,
):
    monkeypatch.delenv("HERMES_KANBAN_TURN_RECOVERY", raising=False)
    classified = classify_api_error(EmptyStreamError(EMPTY_STREAMS[0]), provider="custom", model="nora")
    # What nonretryable_client_error_result books for this verdict.
    result = {"failed": True, "failure_reason": classified.reason.value,
              "failure_retryable": bool(classified.retryable)}
    assert turn_exit_code(result, kanban_worker=True) == KANBAN_RATE_LIMIT_EXIT_CODE
    assert turn_exit_code(result, kanban_worker=False) == 1
    assert should_recover_turn(result, attempt=0, task_id="t_card") is False
    # A retryable verdict would have been rerun in place, up to three times, inside the same run.
    assert should_recover_turn({**result, "failure_retryable": True}, attempt=0, task_id="t_card") is True


def test_a_busy_box_stays_overloaded():
    classified = classify_api_error(
        _Busy("Error code: 503 - llm busy with long-context jobs, retry after 30 s"),
        provider="custom",
        model="nora",
    )
    assert classified.reason is FailoverReason.overloaded
    assert turn_exit_code(
        {"failed": True, "failure_reason": classified.reason.value}, kanban_worker=True
    ) == KANBAN_RATE_LIMIT_EXIT_CODE

