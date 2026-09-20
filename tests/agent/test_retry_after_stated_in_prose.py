"""Neoffice — the wait a provider states in PROSE must be honoured.

Our inference server answers a saturated moment with, verbatim::

    503 {"message": "llm busy with long-context jobs, retry after 5s",
         "type": "service_unavailable", "code": 503}

No ``Retry-After`` header, no ``retry_after`` field: the wait lives only in the
sentence. All three lookups in ``compute_error_backoff`` therefore returned None and
the loop fell back to ``jittered_backoff(base_delay=2.0)`` — measured over 200 draws
per attempt: 2.5s, 2.6s, 4.8s median, so attempts 1 and 2 landed INSIDE the five
seconds the server asked for, 100 % of the time. Two of three retries were spent
hammering a server that had just said « not yet », and the turn died on "API call
failed after 3 retries", which is what the customer reads.

This 503 is not an outage. It is ordinary backpressure, on 15-28 % of the fleet's
text calls in healthy operation.
"""
import pytest

from agent.turn_recovery import _parse_retry_after_from_prose, compute_error_backoff


class _AgentStub:
    """compute_error_backoff only announces; nothing here needs to be real."""

    def __getattr__(self, name):
        return lambda *a, **k: None


class _ProviderError(Exception):
    def __init__(self, body, response=None):
        super().__init__("Error code: 503")
        self.body = {"error": body}
        self.response = response


# The exact body read out of a request dump on osiris.
OLARES_503 = {
    "message": "llm busy with long-context jobs, retry after 5s",
    "type": "service_unavailable",
    "code": 503,
}


@pytest.mark.parametrize(
    "message,attendu",
    [
        ("llm busy with long-context jobs, retry after 5s", 5.0),
        ("llm busy with long-context jobs, retry after 5 s", 5.0),
        ("Server overloaded, retry after 30", 30.0),      # bare number = seconds, as HTTP means it
        ("please retry in 2 minutes", 120.0),
        ("backpressure, retry after 500ms", 0.5),
        ("retry after 1 min", 60.0),
    ],
)
def test_a_stated_wait_is_read(message, attendu):
    assert _parse_retry_after_from_prose(message) == attendu


@pytest.mark.parametrize(
    "message",
    [
        "Internal server error",
        "502 Bad Gateway",
        "the user asked to retry after lunch",   # no number: not a directive
        "",
        None,
        123,
    ],
)
def test_nothing_else_is_read_as_a_wait(message):
    """Reading a number out of prose is only safe while the anchor stays tight."""
    assert _parse_retry_after_from_prose(message) is None


def test_the_real_503_makes_every_retry_wait_what_the_server_asked():
    agent = _AgentStub()
    attentes = [
        compute_error_backoff(
            agent, _ProviderError(OLARES_503), retry_count=n, max_retries=3,
            is_rate_limited=False, is_zai_coding_overload=False,
            base_url="https://example/v1", model="nora",
        )
        for n in range(3)
    ]
    assert attentes == [5.0, 5.0, 5.0], attentes


def test_an_error_without_a_stated_wait_keeps_the_upstream_backoff():
    """The fallback must stay exactly as upstream wrote it when nothing is stated."""
    agent = _AgentStub()
    muet = {"message": "Internal server error", "type": "internal", "code": 500}
    for n in range(3):
        attente = compute_error_backoff(
            agent, _ProviderError(muet), retry_count=n, max_retries=3,
            is_rate_limited=False, is_zai_coding_overload=False,
            base_url="https://example/v1", model="nora",
        )
        # jittered_backoff(base_delay=2.0, max_delay=60.0) — never the flat 5.0 above.
        assert 1.0 <= attente <= 60.0
        assert attente != 5.0


def test_a_header_still_wins_over_the_prose():
    """The prose is the LAST resort; a real Retry-After header must not be overridden."""

    class _Resp:
        headers = {"Retry-After": "12"}

    attente = compute_error_backoff(
        _AgentStub(), _ProviderError(OLARES_503, response=_Resp()), retry_count=0,
        max_retries=3, is_rate_limited=False, is_zai_coding_overload=False,
        base_url="https://example/v1", model="nora",
    )
    assert attente == 12.0
