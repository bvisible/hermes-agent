"""Editing a sales document's lines goes to `ventes`, and the page's pole is honoured (#681).

On 2026-09-24, after two invoice questions answered from the compta pole, Jérémy said
by voice « Ok, est-ce que tu peux rajouter un EAP huit cent trente à cette facture ? ».
No rule matched, so the GO-AHEAD guard read its « Ok » as a confirmation and kept the
message on compta, whose worker put another article on the invoice. Editing a sales
document's lines is a sales gesture (Jérémy's call), and the NORA Live page now passes
the pole its voice server already announced.
"""

import pytest

from gateway.nora_chat_router import _fast_path, classify

AFTER_TWO_INVOICE_QUESTIONS = {"msg": "Ok, et c'est pour qui il y a cette facture?", "pole": "compta"}


def _no_llm(**_kwargs):
    raise AssertionError("the classifier model must not be called")


def test_the_eap830_sentence_reaches_ventes_after_invoice_questions():
    message = "Ok, est-ce que tu peux rajouter un EAP huit cent trente à cette facture?"
    assert _fast_path(message, AFTER_TWO_INVOICE_QUESTIONS) == "ventes"


@pytest.mark.parametrize(
    "message",
    (
        "rajoute deux câbles au devis de Martin",
        "enlève les frais de port de la commande",
        "mets 3 au lieu de 2 sur la facture",
        "change le prix de l'article sur le devis",
        "peux-tu ajouter une remise de 10 % à la commande ?",
        "supprime la ligne des frais de port de cette offre",
    ),
)
def test_a_line_edit_on_a_sales_document_goes_to_ventes(message):
    assert _fast_path(message, None) == "ventes"


@pytest.mark.parametrize(
    "message, pole",
    (
        ("enregistre le paiement de la facture FA-2026-00012", "compta"),
        ("rajoute deux heures sur le devis du chantier PROJ-0091", "projet"),
        ("ajoute une note de frais pour le train de mardi", "rh"),
    ),
)
def test_the_rules_above_keep_their_messages(message, pole):
    assert _fast_path(message, None) == pole


def test_changing_a_documents_state_is_not_a_line_edit():
    assert _fast_path("mets la facture en brouillon", None) != "ventes"


def test_a_real_go_ahead_still_stays_on_the_previous_pole():
    assert _fast_path("oui, envoie", {"msg": "prépare le mail de relance", "pole": "support"}) == "support"


def test_the_page_pole_is_used_when_no_rule_decides():
    pole = classify("Et pour Martin ?", call_llm_fn=_no_llm, main_runtime=None, hint="ventes")
    assert pole == "ventes"


def test_a_rule_still_wins_over_the_page_pole():
    pole = classify("quel est le chiffre d'affaires de septembre ?", call_llm_fn=_no_llm, main_runtime=None, hint="ventes")
    assert pole == "compta"


def test_an_unknown_page_pole_is_ignored():
    calls = []

    def llm(**kwargs):
        calls.append(kwargs)
        raise RuntimeError("classifier unavailable")

    assert classify("Et pour Martin ?", call_llm_fn=llm, main_runtime=None, hint="marketing") == "DIRECT"
    assert calls, "without a valid hint the classifier decides, as before"
