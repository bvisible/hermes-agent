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


# //// Neoffice — the account of an expense is compta's, whatever the expense paid for (09.10). « Dans quel compte
# //// imputer la commission d'un cabinet pour recruter un employé ? » reached rh through the recruitment rule,
# //// which sits above the allocation rule in the table, and the rh pole holds no chart of accounts (gate llm/27).
@pytest.mark.parametrize(
    "message",
    (
        "TEST-IMPUTATION, lecture seule, ne crée et ne modifie rien. Dans notre plan comptable réel, dans quel "
        "compte exact imputer la commission d'un cabinet pour recruter un employé ? Donne le numéro, le libellé "
        "et une justification courte. N'invente aucun compte.",
        "Dans quel compte imputer la commission d'un cabinet de recrutement ?",
        "Sur quel compte passer la facture de l'annonce d'offre d'emploi ?",
        "Quel compte de charge pour les frais de recrutement d'un candidat ?",
    ),
)
def test_the_account_of_a_recruitment_expense_is_compta(message):
    assert _fast_path(message, prior=None) == "compta"


@pytest.mark.parametrize(
    "message",
    (
        "Publie l'offre d'emploi de comptable sur notre site",
        "Combien de candidatures avons-nous reçues pour le poste de comptable ?",
        "Prépare les questions d'entretien pour le candidat au poste de responsable comptable",
    ),
)
def test_recruiting_an_accountant_stays_with_rh(message):
    assert _fast_path(message, prior=None) == "rh"
# //// END Neoffice ////
