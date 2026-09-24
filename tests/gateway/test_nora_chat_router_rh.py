"""The `rh` pole owns expense claims, approvals and payroll recaps — routing regressions.

On 2026-09-23 the rh pole gained the tools to file, list and decide an expense
claim, to show what waits for someone's approval, and the payroll recaps. The
router still described it as « congés, paie, employés, contrats, absences »:
« quelles notes de frais attendent ma validation ? » went to compta, whose worker
hunted the queue with seven list_documents calls, and « qu'est-ce qui attend ma
validation ? » fell to `direct`, handed to the orchestrator 40 s later.
"""

import pytest

from gateway.nora_chat_router import _CLASSIFIER_SYSTEM, _fast_path


@pytest.mark.parametrize(
    "message",
    (
        "j'ai une note de frais de 45 CHF à déposer",
        "ajoute une note de frais pour le train de mardi",
        "quelles notes de frais attendent ma validation ?",
        "où en sont les certificats de salaire ?",
        "quel est le barème de l'impôt à la source à Genève ?",
        "combien de jours de congé me reste-t-il ?",
    ),
)
def test_hr_requests_route_to_rh(message):
    assert _fast_path(message, None) == "rh"


@pytest.mark.parametrize(
    "message",
    (
        "quelles factures fournisseurs sont à payer cette semaine ?",
        "fais un devis pour ce client",
    ),
)
def test_the_rh_rule_does_not_swallow_other_poles(message):
    assert _fast_path(message, None) != "rh"


def test_the_classifier_knows_what_rh_now_covers():
    rh_line = next(line for line in _CLASSIFIER_SYSTEM.splitlines() if line.startswith("- rh :"))
    for term in ("notes de frais", "validation", "paie", "certificats de salaire"):
        assert term in rh_line, term


@pytest.mark.parametrize(
    "message",
    (
        "Le 1er août est-il un jour férié payé ?",
        "combien de jours fériés dans le canton de Vaud ?",
        "le 1er août est payé pour un employé à l'heure ?",
        "Heidi me demande un certificat de travail",
        "un ancien employé veut l'attestation de l'employeur pour le chômage",
        "rédige une attestation de travail pour Marc",
    ),
)
def test_swiss_hr_doctrine_goes_to_rh(message):
    """24.09: a public-holiday question was answered by the orchestrator itself, citing the wrong article."""
    assert _fast_path(message, None) == "rh"


def test_a_date_on_the_first_of_august_is_not_an_hr_matter():
    assert _fast_path("fais une facture pour la livraison du 1er août", None) != "rh"


# //// Neoffice — MY pay is rh's (capability bench, 2026-09-24): « Combien ai-je touché en
# //// août ? » from an employee reached compta and its company revenue summary.
@pytest.mark.parametrize("message", (
    "Combien ai-je touché en août 2026 ?",
    "Combien j'ai gagné le mois passé ?",
    "Montre-moi ma fiche de paie de septembre",
    "Quel est mon salaire net ?",
    "Show me my payslip for August",
    "Zeig mir meine Lohnabrechnung",
    "Quanto è la mia busta paga?",
))
def test_my_pay_goes_to_rh(message):
    from gateway.nora_chat_router import _fast_path

    assert _fast_path(message, prior=None) == "rh"


@pytest.mark.parametrize("message", (
    "Combien ai-je encaissé en août ?",          # the company's money
    "Combien j'ai reçu de paiements ce mois ?",
    "Quel est le chiffre d'affaires d'août ?",
))
def test_the_companys_money_is_not_my_pay(message):
    from gateway.nora_chat_router import _fast_path

    assert _fast_path(message, prior=None) != "rh"
