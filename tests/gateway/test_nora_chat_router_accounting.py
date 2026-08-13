"""Accounting-law routing regressions for the NORA deterministic fast path."""

import pytest

from gateway.nora_chat_router import _fast_path


@pytest.mark.parametrize(
    "message",
    (
        "Quelles sont les obligations en cas de perte de capital selon l'art. 725a CO ?",
        "Explique le surendettement selon l'article 725b CO",
        "Que prévoit 725a CO sans organe de révision ?",
    ),
)
def test_swiss_accounting_law_routes_to_compta(message):
    assert _fast_path(message, prior=None) == "compta"
