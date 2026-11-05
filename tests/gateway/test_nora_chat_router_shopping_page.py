# //// Neoffice — added file (no upstream equivalent): the restaurant's shopping list page tells Nora which list is on screen.
"""On the shopping list page, what the owner says about the list goes to resto (the restaurant pole since 05.11), with or without the words « liste de courses ».

The page declares its list to Nora (doctype « Restaurant Shopping List » and its name, as a page that shows a document does).
« Ajoute cinq kilos d'oignons » said there has no demonstrative (« ce document ») and no keyword, so the classifier was left to
guess: the page is what knows. Only a message that speaks of the list (add, remove, « il me faut ») is taken from the page: a
question about the month's turnover asked on the same page stays with the classifier, as everywhere else.
"""

import pytest

from gateway.nora_chat_router import _DOCTYPE_POLES, _page_document_pole

PAGE = "Restaurant Shopping List"

ON_THE_LIST = [
    "Ajoute cinq kilos d'oignons",
    "Rajoute deux caisses de bière",
    "Mets des torchons",
    "Mets les oignons à huit kilos",
    "Retire les torchons",
    "Enlève la bière",
    "Supprime la ligne des oignons",
    "Il me faut du sel",
    "J'ai besoin de dix kilos de farine",
    "Add five kilos of onions",
    "Remove the tea towels",
    "I need some salt",
    "Füge Mehl hinzu",
    "Aggiungi il sale",
    "Ho bisogno di sale",
]

NOT_ABOUT_THE_LIST = [
    "Quel est mon chiffre d'affaires du mois ?",
    "Montre-moi les factures impayées",
    "Combien me reste-t-il de congés ?",
    "Bonjour",
    "What is the address of the supplier?",
]


def test_the_shopping_list_belongs_to_resto_when_the_classifier_fails():
    assert _DOCTYPE_POLES[PAGE] == "resto"


@pytest.mark.parametrize("message", ON_THE_LIST)
def test_what_is_said_about_the_list_on_its_page_goes_to_resto(message):
    assert _page_document_pole(message, PAGE) == "resto"


@pytest.mark.parametrize("message", NOT_ABOUT_THE_LIST)
def test_anything_else_said_on_the_page_is_left_to_the_classifier(message):
    assert _page_document_pole(message, PAGE) is None


@pytest.mark.parametrize("message", ON_THE_LIST)
def test_the_same_words_on_another_page_are_not_taken_from_the_page(message):
    # an invoice on screen: « ajoute du sel » says nothing about it, the classifier keeps deciding
    assert _page_document_pole(message, "Sales Invoice") is None
    assert _page_document_pole(message, None) is None


def test_a_demonstrative_still_points_at_the_page_as_before():
    assert _page_document_pole("Résume ce document", "Sales Invoice") == "compta"
