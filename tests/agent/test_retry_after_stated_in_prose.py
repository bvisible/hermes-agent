# //// Neoffice — added file (no upstream equivalent): a wait stated only in prose is read
# //// as a last resort when a provider puts it nowhere else.
"""Neoffice — a wait stated only in PROSE is read as a last resort.

Kept honest about what it is: a defensive fallback, NOT a fix for anything observed
on our own provider. The first version of this file claimed the opposite and was
wrong, so the correction lives here where the next reader will find it.

Our inference server answers a saturated moment with::

    503 {"message": "llm busy with long-context jobs, retry after 5s",
         "type": "service_unavailable", "code": 503}

It was believed to carry no ``Retry-After`` header. It does — proven by ``curl -i``
at both hops while the express lane was genuinely saturated — and its body now
carries ``retry_after`` in both structured places as well. The first lookup in
``compute_error_backoff`` therefore already returned 5.0: the real waits were
5.00/5.00/5.00 before this branch existed. The "2.5 / 2.6 / 4.8" first recorded here
came from a stub whose ``response`` was ``None``, an instrument that differed from
production in exactly the field under investigation.

So the prose lookup earns its place only against a future provider that states the
wait and nowhere puts it; while a header is present it is never consulted, and
``test_a_header_still_wins_over_the_prose`` is the test that matters most here.

The real defect this hunt uncovered is elsewhere: 3 retries × 5s = 15s, while two
long prompts hold that lane 30-70s, so a perfectly honoured ``Retry-After`` still
ends in "API call failed after 3 retries". Announced backpressure should not spend
the same 3-strike budget as a real outage. Sizing that needs the lane's occupancy
distribution, so nothing here attempts it.
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
    waits = [
        compute_error_backoff(
            agent, _ProviderError(OLARES_503), retry_count=n, max_retries=3,
            is_rate_limited=False, is_zai_coding_overload=False,
            base_url="https://example/v1", model="nora",
        )
        for n in range(3)
    ]
    assert waits == [5.0, 5.0, 5.0], waits


@pytest.mark.real_retry_backoff  # tests/agent/conftest.py zeroes jittered_backoff otherwise
def test_an_error_without_a_stated_wait_keeps_the_upstream_backoff():
    """The fallback must stay exactly as upstream wrote it when nothing is stated."""
    agent = _AgentStub()
    silent = {"message": "Internal server error", "type": "internal", "code": 500}
    for n in range(3):
        wait = compute_error_backoff(
            agent, _ProviderError(silent), retry_count=n, max_retries=3,
            is_rate_limited=False, is_zai_coding_overload=False,
            base_url="https://example/v1", model="nora",
        )
        # jittered_backoff(base_delay=2.0, max_delay=60.0) — never the flat 5.0 above.
        assert 1.0 <= wait <= 60.0
        assert wait != 5.0


def test_a_header_still_wins_over_the_prose():
    """The prose is the LAST resort; a real Retry-After header must not be overridden."""

    class _Resp:
        headers = {"Retry-After": "12"}

    wait = compute_error_backoff(
        _AgentStub(), _ProviderError(OLARES_503, response=_Resp()), retry_count=0,
        max_retries=3, is_rate_limited=False, is_zai_coding_overload=False,
        base_url="https://example/v1", model="nora",
    )
    assert wait == 12.0


# //// Neoffice — the case that actually describes our provider, added when the header
# //// turned out to be present all along. It locks the ordering AND the value: a real
# //// header is honoured without the prose ever being reached.
def test_our_providers_real_503_is_honoured_from_its_header():
    import httpx

    corps = ('{"error": {"message": "llm busy with long-context jobs, retry after 5s", '
             '"type": "service_unavailable", "code": 503}}')
    requete = httpx.Request("POST", "https://example/v1/chat/completions")
    reponse = httpx.Response(
        503, headers={"content-type": "application/json", "retry-after": "5"},
        content=corps.encode(), request=requete,
    )
    erreur = _ProviderError(reponse)
    erreur.body = {"error": {"message": "llm busy with long-context jobs, retry after 5s",
                             "type": "service_unavailable", "code": 503}}

    waits = [
        compute_error_backoff(
            _AgentStub(), erreur, retry_count=n, max_retries=3,
            is_rate_limited=False, is_zai_coding_overload=False,
            base_url="https://example/v1", model="nora",
        )
        for n in range(3)
    ]
    assert waits == [5.0, 5.0, 5.0], waits


def test_the_503_is_classified_retryable_so_it_reaches_the_backoff():
    """A branch that never reaches compute_error_backoff would make all of this moot."""
    from agent.error_classifier import classify_api_error

    erreur = _ProviderError(None)
    erreur.body = {"error": {"message": "llm busy with long-context jobs, retry after 5s",
                             "type": "service_unavailable", "code": 503}}
    classe = classify_api_error(erreur, provider="custom", model="nora")
    assert classe.should_fallback is False, classe
