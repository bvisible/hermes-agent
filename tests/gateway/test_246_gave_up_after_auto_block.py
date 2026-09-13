"""A tripped circuit breaker must reach the user's desk (tracker #246).

A request routed to a pole crashed three times on the provider; the dispatcher
auto-blocked the task. The desk showed "je transmets votre demande à votre pôle"
and then nothing at all: the `gave_up` event that the breaker emits was filtered
out because the task's status is `blocked`, not `gave_up`. Silence is the worst
possible answer to a customer, so the rule now lives in a pure function and these
cases pin it.
"""

from gateway.kanban_watchers import _gave_up_delivery


def test_circuit_breaker_reaches_the_user():
    """Three crashed runs: status is 'blocked', payload counts the failures."""
    assert (
        _gave_up_delivery(
            "blocked",
            "",
            {"failures": 3, "trigger_outcome": "crashed", "error": "Provider returned an empty response stream"},
        )
        == "breaker"
    )


def test_worker_that_genuinely_gave_up_still_reaches_the_user():
    assert _gave_up_delivery("gave_up", "", {"error": "no answer"}) == "give_up"


def test_a_real_outcome_is_not_doubled():
    """The worker answered: its own event delivered it, this one must stay quiet."""
    assert _gave_up_delivery("blocked", "Voici le devis", {"failures": 3}) is None
    assert _gave_up_delivery("gave_up", "Voici le devis", {}) is None


def test_a_retry_in_flight_says_nothing():
    """Below the threshold the task goes back to ready/review — its outcome will speak."""
    assert _gave_up_delivery("ready", "", {"failures": 1}) is None
    assert _gave_up_delivery("review", "", {"failures": 2}) is None
    assert _gave_up_delivery("running", "", {}) is None


_VIOLATION = "worker exited cleanly (rc=0) without calling kanban_complete or kanban_block — protocol violation."


def test_a_protocol_violation_retry_stays_quiet():
    """While the task goes back to ready, the next run's outcome will speak for it."""
    assert _gave_up_delivery("ready", "", {"failures": 1, "error": _VIOLATION}) is None


def test_a_violation_streak_that_blocks_the_task_reaches_the_user():
    """The dispatcher auto-blocks after a streak of violations: the request is dead (tracker #422).

    Payload observed on osiris 2026-09-13: failures 2 of an effective limit of 3, the
    violation as the error, the task already `blocked`. It used to be filtered as noise,
    and the desk kept only "je transmets votre demande".
    """
    payload = {"failures": 2, "effective_limit": 3, "limit_source": "dispatcher", "error": _VIOLATION}
    assert _gave_up_delivery("blocked", "", payload) == "breaker"


def test_blocked_without_a_failure_count_is_not_a_breaker_trip():
    """A human or a worker blocking with a reason emits its own `blocked` event."""
    assert _gave_up_delivery("blocked", "", {}) is None
    assert _gave_up_delivery("blocked", "", None) is None
