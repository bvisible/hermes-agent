# //// Neoffice — added file (no upstream equivalent): who chases a quotation.
"""« Relance le devis de Martin » is a sales follow-up: ventes holds send_email and the
acceptance link. The dunning rule sent it to compta, whose reminders are for invoices
(capability bench, 2026-09-24)."""
import pytest

from gateway.nora_chat_router import _fast_path


@pytest.mark.parametrize("message, pole", (
    ("Relance le devis de Martin", "ventes"),
    ("Tu peux relancer le client pour le devis DEVIS-2026-00042 ?", "ventes"),
    ("Relance l'offre envoyée à Dupont la semaine dernière", "ventes"),
    ("Relance le devis du chantier PROJ-0057", "projet"),       # the job rule still wins
    ("Relance la facture FA-2026-00123", "compta"),              # an invoice stays a dunning
    ("Envoie une relance de paiement à Dupont", "compta"),
    ("Relance ce prospect", "ventes"),
))
def test_who_chases_what(message, pole):
    assert _fast_path(message, prior=None) == pole
