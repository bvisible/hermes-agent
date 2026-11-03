# //// Neoffice — added file (no upstream equivalent): routing of the card of the restaurant's till built by talking.
"""« J'aimerais des cafés » routes to ventes, the pole that holds the tools that put a step of the card on a card of the chat.

Measured on a development instance, 2026-11-03: the owner's « J'aimerais des cafés » was read as a recurring request and the
orchestrator scheduled « une commande de café quotidienne ». The owner builds the card one piece at a time (« je fais les boissons
froides », « maintenant j'ajoute les vins »): the first turn names the pole, the following ones stay on it.
"""

import pytest

from gateway.nora_chat_router import _CLASSIFIER_SYSTEM, _RESTO_CARD_RE, _fast_path, _keyword_pole

THE_CARD = [
    "J'aimerais des cafés.",
    "j'aimerais des digestifs",
    "Je voudrais des boissons froides",
    "On passe aux plats : des pizzas et du poisson.",
    "Maintenant j'ajoute les vins",
    "Je veux créer ma carte",
    "Crée mes boutons de caisse",
    "Ma caisse est vide",
    "Ajoute des bières à la carte",
    "Je fais les boissons froides",
    # the supplier of a family, said as the owner says it
    "Pour le vin, c'est la Cave régionale.",
    "Le vin, c'est chez Dupont",
    "Mon fournisseur de bière, c'est la Brasserie du Lac",
]

NOT_THE_CARD = [
    # a question
    "Quel est le prix du vin ?",
    "J'aimerais savoir combien de plats j'ai vendus",
    # a document or a figure
    "Ajoute du vin à la commande fournisseur",
    "Fais-moi une facture pour les boissons",
    "Ajoute le plat du jour au devis",
    "Quel est le stock de vin ?",
    # nothing of the card
    "J'aimerais un rendez-vous demain",
    "Crée un client",
    # an opinion is not a supplier
    "Le vin, c'est cher",
    "Le café, c'est le meilleur du monde",
]


@pytest.mark.parametrize("message", THE_CARD)
def test_the_owners_card_goes_to_ventes(message):
    assert _keyword_pole(message) == "ventes"


@pytest.mark.parametrize("message", NOT_THE_CARD)
def test_what_is_not_the_card_is_left_to_the_other_rules(message):
    assert not _RESTO_CARD_RE.search(message)


def test_a_wish_that_comes_back_every_day_is_a_recurring_request_the_model_decides():
    assert _fast_path("Chaque matin j'aimerais des cafés", None) is None


def test_the_classifier_is_told_what_the_owner_says_without_the_word_card():
    assert "CARTE du restaurant" in _CLASSIFIER_SYSTEM
    assert "j'aimerais des cafés" in _CLASSIFIER_SYSTEM
