# //// Neoffice — added file (no upstream equivalent): routing of the restaurant owner's shopping list.
"""« Je fais ma liste de courses » routes to ventes, the pole that holds the tools that propose the list and write it.

Measured on a development instance, 2026-11-03: the classifier answered 'direct' to « Je fais ma liste de courses : il me
faut cinq kilos d'oignons… ». The orchestrator routed it by itself, so the router remembered no pole, and the owner's
« oui, l'eau minérale » after Nora's read-back went 'direct' again: a promise to transmit, no task, nothing written.
A keyword rule routes the first turn, which remembers the pole for the yes.
"""

import pytest

from gateway.nora_chat_router import _CLASSIFIER_SYSTEM, _SHOPPING_LIST_RE, _keyword_pole

SHOPPING_LISTS = [
    "Je fais ma liste de courses : il me faut cinq kilos d'oignons et deux caisses de vin",
    "Ajoute du sel à ma liste de courses",
    "Prépare ma liste d'achat pour demain",
    "Prépare ma liste d’achat pour demain",
    "Fais-moi une liste de courses pour le week-end",
    "Il y a des courses à faire pour jeudi",
    "Add flour to my shopping list",
    "Setz Mehl auf meine Einkaufsliste",
    "Aggiungi il sale alla lista della spesa",
]

NOT_A_SHOPPING_LIST = [
    # a purchase report is compta's, in the plural as in « la liste des achats »
    "Quelle est la liste des achats du mois ?",
    "Montre-moi la liste d'achats de septembre",
    "Fais-moi la liste des factures d'achat",
    "Les courses de livraison de la semaine",
]


@pytest.mark.parametrize("message", SHOPPING_LISTS)
def test_a_shopping_list_goes_to_ventes(message):
    assert _keyword_pole(message) == "ventes"


@pytest.mark.parametrize("message", NOT_A_SHOPPING_LIST)
def test_a_purchase_report_is_not_a_shopping_list(message):
    assert not _SHOPPING_LIST_RE.search(message)


def test_the_classifier_is_told_what_the_owner_says_without_the_word_list():
    # « il me faut des carottes et deux caisses de bière » names no list: the classifier has to know it is the list
    assert "LISTE DE COURSES" in _CLASSIFIER_SYSTEM
    assert "il me faut des carottes" in _CLASSIFIER_SYSTEM
