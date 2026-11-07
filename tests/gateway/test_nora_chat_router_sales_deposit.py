"""A deposit asked from a customer on a quote or a sales order reaches the sales pole (07.11).

//// Neoffice — added file (no upstream equivalent).
« Est-ce que tu peux me faire un acompte sur ce devis ? », asked from the quote, was taken for a
question about what NORA can do and answered « quel client, quel montant ? » without any pole. Only
the sales pole holds frappe_sales_deposit; a deposit to a supplier or a social-insurance instalment
is not a customer deposit.
"""

import pytest

from gateway.nora_chat_router import _fast_path


@pytest.mark.parametrize(
    "message",
    (
        "Est-ce que tu peux me faire un acompte sur ce devis ?",
        "fais-moi un acompte de 30 % sur ce devis",
        "demande un acompte de 500 francs au client sur la commande",
        "crée un acompte de 30% pour ce client",
        "fais une facture d'acompte de 30 % pour Boulangerie Dupont",
        "prépare une demande d’acompte de 500 francs pour Boulangerie Dupont",
    ),
)
def test_a_customer_deposit_goes_to_ventes(message):
    assert _fast_path(message, None) == "ventes"


@pytest.mark.parametrize(
    "message",
    (
        "verse un acompte au fournisseur sur la commande d'achat",
        "l'acompte LPP de mars est-il comptabilisé ?",
        "acompte AVS du personnel",
        "saisis la facture d'acompte du fournisseur de carrelage",
    ),
)
def test_other_instalments_are_not_a_customer_deposit(message):
    assert _fast_path(message, None) != "ventes"
