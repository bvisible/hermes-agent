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
