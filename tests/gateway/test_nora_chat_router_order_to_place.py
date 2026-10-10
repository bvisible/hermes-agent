# //// Neoffice — added file (no upstream equivalent): an order to place reaches the sales pole (10.10).
"""« Recommande-nous 10 raccords coudés en laiton » matched no rule: the classifier read « recommander » as « to
recommend », and the orchestrator answered it itself in 43 s instead of the pole that owns restocking. A verb of
ordering with a quantity, or with « chez le fournisseur », is an order to place.
"""
import pytest

from gateway import nora_chat_router as R


@pytest.mark.parametrize("message", (
    "Recommande-nous 10 raccords coudés en laiton.",
    "Commande 5 arrosoirs en zinc chez le fournisseur.",
    "Commandes-en 10 chez le fournisseur.",
    "Commande-en dix.",
    "Recommande-nous-en 20",
    "Il faut recommander 12 siphons",
    "Commande des cartouches chez notre fournisseur",
    "Passe une commande de 20 siphons au fournisseur",
))
def test_an_order_to_place_reaches_ventes(message):
    assert R._fast_path(message, prior=None) == "ventes"


def test_an_order_after_another_pole_leaves_it():
    assert R._fast_path("Commande-en 10 chez le fournisseur.", prior={"pole": "compta"}) == "ventes"


@pytest.mark.parametrize("message, pole", (
    ("Valide la commande fournisseur PUR-ORD-2026-00012", "compta"),
    ("Fais un graphique des commandes de 10 clients", "analyse"),
))
def test_an_earlier_rule_still_wins(message, pole):
    assert R._fast_path(message, prior=None) == pole


@pytest.mark.parametrize("message", (
    "Combien de commandes en cours ?",
    "Les commandes 2026 sont-elles toutes livrées ?",
    "Recommande-moi un bon fournisseur de vis",
    "La commande d'achat est partie ?",
))
def test_no_order_to_place_without_a_quantity_or_a_supplier(message):
    assert R._ORDER_TO_PLACE_RE.search(message) is None
