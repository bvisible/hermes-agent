# //// Neoffice — added file (no upstream equivalent): an order to place at a supplier is not a visit (#2307).
"""« Qu'est-ce que je dois recommander chez mes fournisseurs d'après mes catalogues ? » went to projet, which has no
catalogue tool: the visit rule (_APPOINTMENT_RE) read « je dois … chez » as a visit, before every other rule, and the
order rule (_ORDER_TO_PLACE_RE) did not take « chez mes fournisseurs ». « mes » is now in the supplier's words, and the
branch of the order rule that names a supplier (_ORDER_AT_A_SUPPLIER_RE) lifts the visit rule, and only it: the other
job rules and the whole table decide after it as before, and the order rule stays last in the table. A quantity alone
lifts nothing. Every phrase of the independent review that must keep its pole is held here, with the pole the router
gave before. Replayed by the rules alone on the 481 real requests of the reference set, no request changes its pole.
"""
import pytest

from gateway import nora_chat_router as R


@pytest.mark.parametrize("msg", [
    "Qu'est-ce que je dois recommander chez mes fournisseurs d'après mes catalogues ?",
    "Je dois recommander 10 raccords chez mes fournisseurs",
    "Je dois commander 20 vis chez mes fournisseurs",
])
def test_an_order_to_place_at_a_supplier_goes_to_ventes(msg):
    assert R._keyword_pole(msg) == "ventes"
    assert R._fast_path(msg, None) == "ventes"


@pytest.mark.parametrize("msg, pole", [
    # a job told, or a job order, with a quantity to order: the job rules still come first
    ("J'ai passé deux heures chez Martin, il faut recommander 2 cartouches", "projet"),
    ("Note 3 heures sur le chantier PROJ-0087 et commande 4 tuiles", "projet"),
    ("Je passe chez Dupont demain à 14h, commande-moi 2 robinets", "projet"),
    ("Je dois passer chez le fournisseur commander 20 vis", "projet"),
    ("Commande 20 plaques de plâtre pour le chantier PROJ-0087", "projet"),
    # the table's earlier rules still win over the order rule
    ("Montre en graphique ce qu'il faut recommander chez nos fournisseurs", "analyse"),
    ("Ce client commande deux fois par mois : montre-moi en graphique ses achats", "analyse"),
    ("Écris un mail à notre fournisseur pour commander 20 vis", "support"),
    ("Le client Dupont commande 3 fois par an, relance-le pour sa facture impayée", "compta"),
    ("Pour 40 couverts mardi, il faut commander 6 kg de riz", "resto"),
    ("Il faut commander 3 kg de farine pour la recette de la tarte", "resto"),
    # a visit with no order to place at a supplier stays a visit
    ("je passe chez mes fournisseurs demain", "projet"),
    ("rendez-vous chez le fournisseur pour recommander les vis", "projet"),
    ("je passe chez le client demain à 14h", "projet"),
    ("je dois passer chez Dupont demain", "projet"),
    ("Fixe un rendez-vous avec le client Dupont", "projet"),
    ("planifie une visite chez le fournisseur Martin", None),
    ("commande 15 unités chez notre fournisseur", "ventes"),
])
def test_every_other_phrase_keeps_its_pole(msg, pole):
    assert R._fast_path(msg, None) == pole


def test_the_order_rule_is_the_one_of_before_with_mes():
    assert "nos|mes|ce" in R._ORDER_TO_PLACE_RE.pattern
    assert R._ORDER_AT_A_SUPPLIER_RE.pattern == R._ORDER_VERB + R._AT_A_SUPPLIER
    assert [pole for rx, pole in R._FAST_PATH_RULES if rx is R._ORDER_TO_PLACE_RE] == ["ventes"]
