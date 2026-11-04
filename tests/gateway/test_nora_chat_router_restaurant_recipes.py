# //// Neoffice — added file (no upstream equivalent): routing of the restaurant owner's recipes.
"""« Note la recette du risotto : pour quatre, 320 g de riz... » routes to ventes, the pole that holds the tools that propose the recipe and write it.

Same reason as the menus: left to the orchestrator the first turn remembers no pole and the owner's « oui » reaches nobody. But « recettes » is
also what a till took in: the takings of the month, of the day, of the till stay with compta.
"""

import pytest

from gateway.nora_chat_router import (
    _CLASSIFIER_SYSTEM,
    _RESTO_PORTIONS_RE,
    _RESTO_RECIPE_RE,
    _RESTO_SEASON_RE,
    _keyword_pole,
)

RECIPES = [
    "Note la recette du risotto aux champignons : pour quatre, 320 g de riz, un litre de bouillon",
    "Ma recette de tarte aux pommes : 250 g de farine, 125 g de beurre, 4 pommes",
    "Quelles recettes de soupe j'ai ?",
    "Ajoute 200 ml de crème à la recette de la soupe de courge",
    "La soupe du jour, c'est la recette de la soupe de courge",
    "Donne-moi la recette du gratin pour 40 portions",
    "Valide ma recette de salade grecque",
]

PORTIONS = [
    "La soupe de courge pour 40 couverts",
    "Combien de courge me faut-il pour 25 portions ?",
]

TAKINGS = [
    "Quelles sont les recettes du mois ?",
    "Les recettes de la journée à la caisse",
    "Le chiffre d'affaires et les recettes de la boutique",
    "Montre-moi les recettes de la semaine",
    "Total des recettes de l'année et des dépenses",
]


@pytest.mark.parametrize("message", RECIPES)
def test_a_recipe_goes_to_ventes(message):
    assert _RESTO_RECIPE_RE.search(message)
    assert _keyword_pole(message) == "ventes"


@pytest.mark.parametrize("message", PORTIONS)
def test_a_number_of_covers_asked_of_a_dish_goes_to_ventes(message):
    assert _RESTO_PORTIONS_RE.search(message)
    assert _keyword_pole(message) == "ventes"


@pytest.mark.parametrize("message", TAKINGS)
def test_the_takings_of_a_till_are_not_a_recipe(message):
    assert not _RESTO_RECIPE_RE.search(message)
    assert _keyword_pole(message) != "ventes" or "recette" not in message.lower()


def test_the_classifier_is_told_what_the_owner_says_of_his_recipes_and_that_takings_are_compta():
    assert "RECETTES de cuisine" in _CLASSIFIER_SYSTEM
    assert "note la recette du risotto" in _CLASSIFIER_SYSTEM
    assert "encaissements" in _CLASSIFIER_SYSTEM and "compta" in _CLASSIFIER_SYSTEM


SEASON = [
    "Qu'est-ce qui est de saison en novembre ?",
    "Les asperges, c'est de saison ?",
    "Propose-moi des recettes de saison",
    "Quels légumes sont de saison en ce moment ?",
    "Une soupe de saison pour le menu du midi",
    "What vegetables are in season in March?",
    "Des fraises hors saison, on en met à la carte ?",
    "Dis-moi quoi de saison",
]

NOT_A_KITCHEN = [
    "Les articles de saison de la boutique",
    "Le chiffre d'affaires de la saison",
    "Une promotion de fin de saison",
    "Les soldes de fin de saison",
]


@pytest.mark.parametrize("message", SEASON)
def test_the_season_of_a_product_or_a_dish_goes_to_ventes(message):
    assert _RESTO_SEASON_RE.search(message)
    assert _keyword_pole(message) == "ventes"


@pytest.mark.parametrize("message", NOT_A_KITCHEN)
def test_a_season_that_is_not_a_kitchen_one_is_not_routed_by_this_rule(message):
    assert not _RESTO_SEASON_RE.search(message)


def test_the_classifier_is_told_the_season_is_ventes():
    assert "SAISON des produits" in _CLASSIFIER_SYSTEM
