"""Neoffice — an announced, self-resolving unavailability needs its own budget.

A provider that ANSWERS "retry after 5s" is telling us it is alive and busy. One that
is simply broken tells us nothing. Upstream spends the same three-strike budget on
both, and that is what kills turns: measured on osiris across every agent log since
17.08, the express lane turned away 336 calls and **196 of them (58 %) died** -- with
the Retry-After honoured every single time (537 sleeps of exactly 5.0s out of 545).
Three attempts are only TWO waits, so the real patience was 10 seconds against a lane
that two long prompts hold for 30 to 70.

Recovery hazard per 5s wait: 25.0 % then 22.2 % -- flat, not decaying, which fits
336 x 0.765^2 = 196.6 against the 196 observed.

The budget is OFF by default and these tests pin that: nothing changes until someone
sets a number, and that number belongs with the lane's occupancy distribution in hand.
"""
import httpx
import pytest

from agent.retry_utils import announced_wait_kind, announced_wait_retry_ceiling


def _error(body, header=None):
    err = Exception("boom")
    err.body = body
    request = httpx.Request("POST", "https://example/v1/chat/completions")
    headers = {"content-type": "application/json"}
    if header is not None:
        headers["retry-after"] = str(header)
    err.response = httpx.Response(503, headers=headers, content=b"{}", request=request)
    return err


BUSY = {"error": {"message": "llm busy with long-context jobs, retry after 5s",
                  "type": "service_unavailable", "code": 503, "retry_after": 5},
        "retry_after": 5}
RESTARTING = {"error": {"message": "the model backend is unreachable",
                        "type": "upstream_unavailable", "code": 503, "retry_after": 20},
              "retry_after": 20}
DEAD = {"error": {"message": "502 Bad Gateway", "type": "internal", "code": 502}}


@pytest.mark.parametrize(
    "body,attendu",
    [
        (BUSY, "service_unavailable"),
        (RESTARTING, "upstream_unavailable"),
        (DEAD, None),          # a plain 502 announces nothing
        ({}, None),
        ({"error": "not a dict"}, None),
    ],
)
def test_only_an_announced_wait_is_recognised(body, attendu):
    """Read from the provider's own `type`, never guessed from a status code: a 503
    alone does not say whether the far side is busy or broken."""
    assert announced_wait_kind(_error(body)) == attendu


def test_a_non_dict_body_is_not_a_kind():
    err = Exception("boom")
    err.body = "plain text"
    assert announced_wait_kind(err) is None
    assert announced_wait_kind(Exception("no body at all")) is None


# //// The floor is what keeps this from EVER shortening the normal loop.
@pytest.mark.parametrize(
    "budget,wait,plafond",
    [
        (0.0, 5.0, 3),      # off: today's behaviour, 2 waits = 10s
        (0.0, 20.0, 3),     # off: 2 waits = 40s
        (45.0, 5.0, 10),    # 9 waits = 45s
        (45.0, 20.0, 3),    # 2 waits = 40s -- the budget never shortens anything
        (120.0, 5.0, 25),
        (120.0, 20.0, 7),
        (45.0, 0.0, 3),     # no announced wait: nothing to budget against
        (-5.0, 5.0, 3),     # a negative budget is off, not a shrink
    ],
)
def test_the_ceiling_spends_the_budget_and_never_less(budget, wait, plafond):
    assert announced_wait_retry_ceiling(budget, wait, floor=3) == plafond


def test_a_budget_of_zero_is_exactly_todays_behaviour():
    """The whole safety property in one assertion: no number set, no change."""
    for wait in (1.0, 5.0, 20.0, 60.0):
        assert announced_wait_retry_ceiling(0.0, wait, floor=3) == 3


def test_the_ceiling_respects_a_raised_floor():
    """Another rule may already have widened the loop; this must not undo it."""
    assert announced_wait_retry_ceiling(45.0, 20.0, floor=8) == 8
    assert announced_wait_retry_ceiling(0.0, 5.0, floor=8) == 8


def test_the_budget_buys_the_patience_the_measurement_asks_for():
    """45s was derived from the hazard fit; this pins the arithmetic behind it.

    Ceiling N means N-1 waits are actually slept: the loop gives up when
    retry_count >= ceiling, BEFORE computing that attempt's backoff.
    """
    plafond = announced_wait_retry_ceiling(45.0, 5.0, floor=3)
    assert (plafond - 1) * 5.0 == 45.0


def test_a_dead_engine_gets_no_extra_patience():
    """The reason this is a separate counter and not a bigger api_max_retries."""
    assert announced_wait_kind(_error(DEAD)) is None


# //// Neoffice — the WIRING, exercised for real. The arithmetic above is pure; this is
# //// the part that reads the wait off a live error object and off the agent's config,
# //// and it is the part that would have hidden a typo: it only runs once somebody sets
# //// a budget, which nobody has yet.
from agent.turn_recovery import announced_wait_ceiling_for  # noqa: E402


class _Agent:
    def __init__(self, budget=None):
        if budget is not None:
            self._announced_wait_budget_seconds = budget


def test_no_budget_configured_changes_nothing():
    """The default. An agent that has never heard of this keeps its three strikes."""
    assert announced_wait_ceiling_for(_Agent(), _error(BUSY, header=5), 3) == 3
    assert announced_wait_ceiling_for(_Agent(0.0), _error(BUSY, header=5), 3) == 3


def test_a_budget_widens_only_the_announced_error():
    agent = _Agent(45.0)
    assert announced_wait_ceiling_for(agent, _error(BUSY, header=5), 3) == 10
    # A dead engine announces nothing: it keeps the short, blind budget.
    assert announced_wait_ceiling_for(agent, _error(DEAD), 3) == 3


def test_the_wait_is_read_from_the_body_when_no_header_is_present():
    """Olares now carries retry_after in both structured places; either must work."""
    agent = _Agent(45.0)
    assert announced_wait_ceiling_for(agent, _error(BUSY), 3) == 10          # body only
    assert announced_wait_ceiling_for(agent, _error(BUSY, header=5), 3) == 10  # header too


def test_the_header_and_the_body_agree_on_the_restart_case():
    agent = _Agent(120.0)
    assert announced_wait_ceiling_for(agent, _error(RESTARTING, header=20), 3) == 7


def test_an_announced_error_with_no_readable_wait_changes_nothing():
    """Announcing a type without a duration buys nothing: there is no budget to spend."""
    muet = {"error": {"message": "busy", "type": "service_unavailable", "code": 503}}
    assert announced_wait_ceiling_for(_Agent(45.0), _error(muet), 3) == 3


def test_it_never_shortens_a_loop_another_rule_widened():
    """Z.AI's own ceiling (8) must survive a narrower announced budget."""
    assert announced_wait_ceiling_for(_Agent(45.0), _error(RESTARTING, header=20), 8) == 8
