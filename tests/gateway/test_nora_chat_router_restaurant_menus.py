# //// Neoffice — added file (no upstream equivalent): routing of the restaurant owner's menus and of the cooking / side of his dishes.
"""« Compose-moi un menu du soir à 38 francs » routes to ventes, the pole that holds the tools that propose the menu and write it.

Measured on a development instance, 2026-11-04: the classifier answered 'direct' to the menu sentence. The orchestrator routed it by
itself, so the router remembered no pole, and the owner's « oui, écris-le » went to nobody (an empty promise, nothing written).
A keyword rule routes the first turn, which remembers the pole for the yes.
"""

import pytest

from gateway.nora_chat_router import _CLASSIFIER_SYSTEM, _RESTO_MENU_RE, _RESTO_OPTIONS_RE, _keyword_pole

MENUS = [
    "Compose-moi un menu du soir à 38 francs : en entrée le velouté de courge, en plat le magret de canard, en dessert la panna cotta.",
    "Crée un menu du midi avec le plat du jour et un café, 24 francs",
    "Ajoute une soupe au menu du midi",
    "Désactive le menu du jour",
    "Mets le menu enfant à 14 francs",
    "Fais-moi une formule à 19 CHF : plat et dessert",
    "Enlève le café du menu du soir",
]

NOT_A_MENU = [
    # the menu of a screen
    "Ajoute un menu dans la sidebar",
    "Crée un raccourci dans le menu de l'espace de travail",
    "Mets le menu du desk à gauche",
    # a formula of a spreadsheet, a polite formula
    "Quelle formule Excel pour additionner les plats de la colonne B ?",
    "Une formule de politesse pour finir ma lettre au client",
]

OPTIONS = [
    "Il faut une cuisson sur les viandes et un accompagnement",
    "Mets une cuisson sur l'entrecôte",
    "Les plats de poisson viennent avec un accompagnement",
]


@pytest.mark.parametrize("message", MENUS)
def test_a_menu_goes_to_ventes(message):
    assert _RESTO_MENU_RE.search(message)
    assert _keyword_pole(message) == "ventes"


@pytest.mark.parametrize("message", NOT_A_MENU)
def test_the_menu_of_a_screen_or_a_formula_is_not_a_restaurant_menu(message):
    assert not _RESTO_MENU_RE.search(message)


@pytest.mark.parametrize("message", OPTIONS)
def test_the_cooking_and_the_side_of_the_dishes_go_to_ventes(message):
    assert _RESTO_OPTIONS_RE.search(message)
    assert _keyword_pole(message) == "ventes"


def test_the_classifier_is_told_what_the_owner_says_of_his_menus():
    assert "MENUS / FORMULES" in _CLASSIFIER_SYSTEM
    assert "menu du soir à 38 francs" in _CLASSIFIER_SYSTEM
    assert "CUISSONS" in _CLASSIFIER_SYSTEM and "ACCOMPAGNEMENTS" in _CLASSIFIER_SYSTEM
