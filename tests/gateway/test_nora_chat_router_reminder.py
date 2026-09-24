# //// Neoffice — added file (no upstream equivalent): a one-off reminder goes to NORA
# //// herself, who holds nora_reminder_create; a payment reminder stays compta.
"""« Rappelle-moi demain à 10 h » is routed to NORA, not read as a payment reminder."""
import pytest

from gateway.nora_chat_router import _fast_path


@pytest.mark.parametrize("message", (
    "Rappelle-moi demain à 10h d'appeler Dupont",
    "Mets-moi un rappel lundi à 8 h pour la TVA",
    "rappelle-moi dans 2 heures de relancer le client Martin",
    "Fais-moi penser cet après-midi à envoyer le devis",
    "Crée un rappel le 3 octobre : renouveler l'assurance",
    "Erinnere mich morgen um 9 Uhr an die Sitzung",
    "Remind me tomorrow at 10:00 to call the bank",
))
def test_a_one_off_reminder_goes_to_nora(message):
    assert _fast_path(message, prior=None) == "DIRECT"


def test_it_wins_over_the_conversation_context():
    assert _fast_path("Rappelle-moi demain à 9h de payer Sunrise", prior={"pole": "compta"}) == "DIRECT"


@pytest.mark.parametrize("message, expected", (
    ("Crée un rappel de paiement pour Dupont demain", "compta"),
    ("Envoie un rappel de facture à Martin", "compta"),
))
def test_a_payment_reminder_stays_compta(message, expected):
    assert _fast_path(message, prior=None) == expected


@pytest.mark.parametrize("message", (
    "Rappelle-moi tous les lundis à 8h de faire la TVA",   # repeated → the classifier's 'recurrent'
    "Rappelle-moi combien on a facturé en août",           # « tell me again », no moment
))
def test_not_a_one_off_reminder(message):
    assert _fast_path(message, prior=None) != "DIRECT"


def test_a_short_go_ahead_after_a_compta_proposal_stays_compta():
    assert _fast_path("ok crée le rappel", prior={"pole": "compta"}) == "compta"
