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


# //// Neoffice — allocation must not depend on the remote classifier. ////
@pytest.mark.parametrize(
    "message",
    (
        "Dans notre plan comptable réel, où passer cette dépense ?",
        "Dans quel compte imputer une facture Google Ads ?",
        "Quel compte de charge utiliser pour cet achat ?",
        "Quelle imputation comptable pour ce ticket ?",
    ),
)
def test_invoice_allocation_routes_to_compta_without_classifier(message):
    assert _fast_path(message, prior=None) == "compta"


@pytest.mark.parametrize(
    "message",
    (
        "À qui imputer cette erreur ?",
        "Comment changer mon compte utilisateur ?",
    ),
)
def test_non_accounting_account_words_do_not_route_to_compta(message):
    assert _fast_path(message, prior=None) != "compta"
# //// END Neoffice ////
