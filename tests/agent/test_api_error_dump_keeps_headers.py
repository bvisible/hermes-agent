"""Neoffice — the provider-error dump must record the response HEADERS.

This dump is the only forensic record of a provider error. It captured the status
and the body while discarding the headers -- which is exactly where the first and
most authoritative lookup in ``compute_error_backoff`` reads ``Retry-After``.

When our inference server started answering saturation with

    503 {"message": "llm busy with long-context jobs, retry after 5s"}

and the retries kept firing inside the window it had asked us to stay out of, the
61 dumps of that error could establish that the response object HAD survived the
streaming path -- every one of them carries ``response_status`` -- but not whether
the header was ever present. The question could not be settled from what had been
recorded. An instrument that drops the field under investigation turns a
five-minute answer into a guess.
"""
from agent.agent_runtime_helpers import _api_error_debug_info


class _Response:
    status_code = 503
    text = '{"error": {"message": "llm busy with long-context jobs, retry after 5s"}}'

    def __init__(self, headers):
        self.headers = headers


class _ProviderError(Exception):
    def __init__(self, response=None):
        super().__init__("Error code: 503")
        self.response = response
        self.status_code = 503


def test_the_dump_records_retry_after():
    info = _api_error_debug_info(_ProviderError(_Response({"Retry-After": "5"})))
    assert info["response_headers"]["Retry-After"] == "5"
    # The fields that were already recorded must stay.
    assert info["response_status"] == 503
    assert "llm busy" in info["response_text"]


def test_header_names_and_values_are_stringified():
    """A provider's header map is not always a plain dict of str."""
    info = _api_error_debug_info(_ProviderError(_Response({b"Retry-After": 5})))
    assert all(isinstance(k, str) and isinstance(v, str)
               for k, v in info["response_headers"].items())


def test_an_error_without_a_response_still_dumps():
    """No response object is the ordinary case for a synthesized error."""
    info = _api_error_debug_info(_ProviderError(None))
    assert "response_headers" not in info
    assert info["type"] == "_ProviderError"


def test_an_unreadable_header_map_does_not_break_the_dump():
    """The dump fires on every API error: it may never be the thing that raises."""

    class _Hostile:
        status_code = 503
        text = "{}"

        @property
        def headers(self):
            raise RuntimeError("no headers for you")

    info = _api_error_debug_info(_ProviderError(_Hostile()))
    assert info["response_status"] == 503
    assert "response_headers" not in info
