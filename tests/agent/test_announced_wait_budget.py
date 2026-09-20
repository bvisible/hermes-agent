"""Neoffice — an announced, self-resolving unavailability gets its own budget.

A provider that ANSWERS "retry after 5s" is telling us it is alive and busy. One that
is simply broken tells us nothing. Upstream spends the same three-strike budget on
both, and that is what kills turns: measured on osiris across every agent log since
17.08, the express lane turned away 336 calls and 196 of them (58 %) died -- with the
Retry-After honoured every single time. Three attempts are only TWO waits, so the real
patience was 10 seconds.

The lane's occupancy was then measured on the provider's own side, 14 days and 114 876
chat calls: median 9s, p90 22, p95 31, max 97 -- so 30s of patience covers 95 % of
episodes and 45s covers 99 %.

And the wait became VARIABLE: the provider now computes each Retry-After from the
shortest remaining long job, bounded to [2, 30]s (observed 10, then 4, then 6 inside
one episode). That is why the budget is denominated in SECONDS and tallied across the
turn. Counting attempts would be right only while every wait is identical, which it no
longer is.

Off by default, and these tests pin it: nothing changes until someone sets a number.
"""
import httpx
import pytest

from agent.retry_utils import (
    announced_wait_kind,
    announced_wait_retry_ceiling,
    announced_wait_spend,
)
from agent.turn_recovery import announced_wait_ceiling_for


def _error(body, header=None):
    err = Exception("boom")
    err.body = body
    request = httpx.Request("POST", "https://example/v1/chat/completions")
    headers = {"content-type": "application/json"}
    if header is not None:
        headers["retry-after"] = str(header)
    err.response = httpx.Response(503, headers=headers, content=b"{}", request=request)
    return err


def _busy(wait=5):
    return {"error": {"message": "llm busy with long-context jobs", "code": 503,
                      "type": "service_unavailable", "retry_after": wait},
            "retry_after": wait}


def _restarting(wait=20):
    return {"error": {"message": "the model backend is unreachable", "code": 503,
                      "type": "upstream_unavailable", "retry_after": wait},
            "retry_after": wait}


DEAD = {"error": {"message": "502 Bad Gateway", "type": "internal", "code": 502}}


class _Agent:
    def __init__(self, busy=0.0, restart=0.0):
        self._announced_wait_budget_seconds = busy
        self._announced_restart_budget_seconds = restart


# ---------------------------------------------------------------- what is announced

@pytest.mark.parametrize(
    "body,attendu",
    [
        (_busy(), "service_unavailable"),
        (_restarting(), "upstream_unavailable"),
        (DEAD, None),                       # a plain 502 announces nothing
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


# ---------------------------------------------------------------- the tally

@pytest.mark.parametrize(
    "spent,wait,budget,attendu",
    [
        (0.0, 5.0, 30.0, 5.0),
        (25.0, 5.0, 30.0, 30.0),      # exactly on budget still counts
        (26.0, 5.0, 30.0, 26.0),      # would overrun: unchanged
        (0.0, 5.0, 0.0, 0.0),         # off
        (0.0, 0.0, 30.0, 0.0),        # nothing announced
    ],
)
def test_the_tally_advances_only_while_the_budget_covers_the_wait(spent, wait, budget, attendu):
    assert announced_wait_spend(spent, wait, budget) == attendu


# ---------------------------------------------------------------- the ceiling

def test_the_ceiling_grants_one_more_attempt_at_a_time():
    """One past the attempt being decided: the loop gives up when retry_count >= ceiling
    BEFORE computing that attempt's backoff."""
    assert announced_wait_retry_ceiling(30.0, 5.0, floor=3, retry_count=2) == 4
    assert announced_wait_retry_ceiling(30.0, 5.0, floor=3, retry_count=5) == 7


def test_the_ceiling_never_shortens_anything():
    assert announced_wait_retry_ceiling(0.0, 5.0, floor=3, retry_count=9) == 3
    assert announced_wait_retry_ceiling(30.0, 5.0, floor=8, retry_count=1) == 8
    assert announced_wait_retry_ceiling(30.0, 5.0, floor=3, retry_count=0,
                                        already_spent=28.0) == 3


# ---------------------------------------------------------------- a whole turn

def _tour(agent, delais, max_retries=3):
    """Replay one turn: return the seconds actually slept before the loop gives up."""
    cumul = 0.0
    for n, attente in enumerate(delais):
        plafond = announced_wait_ceiling_for(agent, _error(_busy(attente), header=attente),
                                             max_retries, n)
        if n >= plafond:
            break
        cumul += attente
    return cumul


def test_no_budget_is_exactly_todays_behaviour():
    """Three attempts, whatever the provider announces."""
    agent = _Agent()
    assert _tour(agent, [10, 4, 6, 5, 8, 12]) == 20.0   # 3 attempts, then out


def test_a_budget_is_spent_in_seconds_across_a_variable_sequence():
    """The real sequence observed in one saturated episode: 10, then 4, then 6."""
    agent = _Agent(busy=30.0)
    # 10 + 4 + 6 + 5 = 25; the next announced 8 would reach 33 and is refused.
    assert _tour(agent, [10, 4, 6, 5, 8, 12]) == 25.0


def test_a_constant_wait_spends_the_budget_exactly():
    agent = _Agent(busy=30.0)
    assert _tour(agent, [5] * 12) == 30.0


def test_the_budget_is_per_turn_not_per_session():
    """retry_count 0 restarts the tally; otherwise one bad turn would exhaust the day."""
    agent = _Agent(busy=30.0)
    assert _tour(agent, [5] * 12) == 30.0
    assert _tour(agent, [5] * 12) == 30.0


def test_a_restart_has_its_own_budget_and_it_is_off():
    """No distribution exists for an engine restart, so it gets no number -- giving it
    the lane's would be a guess dressed as a measurement."""
    agent = _Agent(busy=30.0)          # the busy budget must not leak across
    for n, attente in enumerate((20, 20, 20, 20)):
        plafond = announced_wait_ceiling_for(
            agent, _error(_restarting(attente), header=attente), 3, n)
        assert plafond == 3, (n, plafond)


def test_a_dead_engine_gets_no_extra_patience():
    """The reason this is a separate counter and not a bigger api_max_retries: raising
    that would also lengthen the wait in front of an engine that is simply broken."""
    agent = _Agent(busy=45.0, restart=120.0)
    for n in range(5):
        assert announced_wait_ceiling_for(agent, _error(DEAD), 3, n) == 3


def test_an_announced_error_with_no_readable_wait_changes_nothing():
    """Announcing a type without a duration buys nothing: there is no budget to spend."""
    muet = {"error": {"message": "busy", "type": "service_unavailable", "code": 503}}
    assert announced_wait_ceiling_for(_Agent(busy=30.0), _error(muet), 3, 0) == 3


def test_the_wait_is_read_from_the_body_when_no_header_is_present():
    """Both structured places must work: the provider carries it in header AND body."""
    agent = _Agent(busy=30.0)
    assert announced_wait_ceiling_for(agent, _error(_busy(5)), 3, 2) == 4        # body
    agent = _Agent(busy=30.0)
    assert announced_wait_ceiling_for(agent, _error(_busy(5), header=5), 3, 2) == 4
