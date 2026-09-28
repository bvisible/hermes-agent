# //// Neoffice — added file (no upstream equivalent): a message about the document open
# //// on the page goes to that document's pole, before the classifier (#914, 2026-09-28).
"""« Résume ce document » on a journal entry goes to compta, not where the classifier guesses.

On 28.09 the workspace panel's « Résumer ce document », sent from a journal entry, was
classified « support » in 2.8 s; a cold support worker answered « lequel ? » after 57 s.
"""
import pytest

from gateway.nora_chat_router import _page_document_pole, classify


def _no_llm(**_kwargs):
    raise AssertionError("the classifier must not be called")


class _Reply:
    def __init__(self, text):
        msg = type("M", (), {"content": text})()
        self.choices = [type("C", (), {"message": msg})()]


def _llm_says(word):
    calls = []

    def fn(**kwargs):
        calls.append(kwargs)
        return _Reply(word)

    return fn, calls


@pytest.mark.parametrize(
    "message, doctype, pole",
    [
        ("Résume ce document", "Journal Entry", "compta"),
        ("Explique-moi cette écriture", "Journal Entry", "compta"),
        ("Que dit cette facture ?", "Sales Invoice", "compta"),
        ("Résume le document ouvert", "Purchase Invoice", "compta"),
        ("Summarize this document", "Quotation", "ventes"),
        ("What is this invoice about?", "Sales Invoice", "compta"),
        ("Fasse dieses Dokument zusammen", "Sales Invoice", "compta"),
        ("Riassumi questo documento", "Project", "projet"),
        ("Résume ce document", "Leave Application", "rh"),
        ("Résume ce ticket", "HD Ticket", "support"),
    ],
)
def test_a_message_pointing_at_the_page_goes_to_its_documents_pole(message, doctype, pole):
    # The classifier would say « support », as on 28.09: the page decides before it is asked.
    fn, calls = _llm_says("support" if pole != "support" else "ventes")
    assert classify(message, call_llm_fn=fn, main_runtime=None, page_doctype=doctype) == pole
    assert calls == []


def test_without_a_document_open_the_classifier_decides():
    fn, calls = _llm_says("support")
    assert classify("Résume ce document", call_llm_fn=fn, main_runtime=None, page_doctype=None) == "support"
    assert len(calls) == 1


def test_a_document_type_with_no_pole_leaves_it_to_the_classifier():
    fn, calls = _llm_says("ventes")
    assert classify("Résume ce document", call_llm_fn=fn, main_runtime=None, page_doctype="ToDo") == "ventes"
    assert len(calls) == 1


@pytest.mark.parametrize(
    "message",
    ["Combien de devis ai-je envoyés ce mois ?", "Montre-moi les écritures de septembre", "How many invoices are overdue?"],
)
def test_a_question_that_does_not_point_at_the_page_is_not_decided_by_the_page(message):
    assert _page_document_pole(message, "Journal Entry") is None


def test_the_nora_live_hint_still_comes_first():
    assert (
        classify("Résume ce document", call_llm_fn=_no_llm, main_runtime=None, hint="ventes", page_doctype="Journal Entry")
        == "ventes"
    )
