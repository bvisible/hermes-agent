# //// Neoffice — added file (no upstream equivalent): validating a document goes to a pole that can.
"""« Tu peux la valider puis envoyer le mail » stays with the pole that offered it (07.10).

A colleague's demonstration on the development instance: the sales pole drafted an invoice and offered « Voulez-vous
que je la valide et que je lui envoie un email ? »; the yes, which named the mail, was sent to the support pole by
the e-mail rule, and support holds no tool to validate a document.
"""
import pytest

from gateway.nora_chat_router import _fast_path


def test_the_observed_yes_stays_with_the_pole_that_offered():
    assert _fast_path("Tu peux la valider puis envoyer le mail s'il te plait.", prior={"pole": "ventes"}) == "ventes"


@pytest.mark.parametrize("prior", ("ventes", "compta", "projet"))
def test_validate_then_mail_stays_on_the_document_pole(prior):
    assert _fast_path("Oui, valide-la et envoie-la par mail", prior={"pole": prior}) == prior


def test_a_long_validation_follow_up_stays_too():
    message = "Merci, est-ce que tu peux maintenant valider cette facture et ensuite envoyer le mail au client avec le PDF ?"
    assert len(message) > 80
    assert _fast_path(message, prior={"pole": "ventes"}) == "ventes"


@pytest.mark.parametrize("message, expected", (
    ("Valide la facture FA-2026-02448", "ventes"),
    ("Valide la facture FA-2026-02448 et envoie-la par mail à Daniel", "ventes"),
    ("Valide le devis DEVIS-2026-01274", "ventes"),
    ("Valide la commande BC-2026-00489", "ventes"),
    ("Valide le bon de livraison de Martin", "ventes"),
    ("Valide la facture fournisseur de Sunrise", "compta"),
    ("Valide le paiement de Daniel Moret", "compta"),
))
def test_a_validation_asked_cold_reaches_a_pole_that_validates(message, expected):
    assert _fast_path(message, prior=None) == expected


@pytest.mark.parametrize("message, expected", (
    ("Valide la note de frais de Paul", "rh"),
    ("Valide le congé de Marie", "rh"),
))
def test_what_hr_decides_stays_with_hr(message, expected):
    assert _fast_path(message, prior=None) == expected


def test_a_free_email_still_reaches_support():
    assert _fast_path("Envoie un mail à Martin pour le remercier", prior=None) == "support"
    assert _fast_path("Ok, écris un mail à Martin pour le remercier", prior={"pole": "ventes"}) == "support"


def test_a_bare_go_ahead_after_a_proposal_is_unchanged():
    assert _fast_path("ok valide", prior={"pole": "compta"}) == "compta"
# //// END Neoffice ////
