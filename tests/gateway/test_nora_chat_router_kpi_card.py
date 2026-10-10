# //// Neoffice — added file (no upstream equivalent): a KPI card asked for reaches analyse (10.10).
"""« Crée-moi un indicateur du chiffre d'affaires du mois » matched only the revenue rule and reached compta,
which told the figure in a sentence: no card was made. A KPI card to create is analyse's, before the revenue
rule; a sentence that only uses the word « indicateur » keeps its pole.
"""
import pytest

from gateway import nora_chat_router as R


@pytest.mark.parametrize("message", (
    "Crée-moi un indicateur du chiffre d'affaires du mois",
    "Fais-moi un indicateur des ventes de l'année",
    "Ajoute une carte d'indicateur des factures en retard",
    "Mets un indicateur du chiffre d'affaires sur mon tableau",
    "Montre-moi mes KPI du mois",
    "Create a KPI card for the revenue of the month",
    "Add an indicator of the overdue invoices",
    "Erstelle eine Kennzahl für den Umsatz des Monats",
    "Crea un indicatore del fatturato del mese",
))
def test_a_kpi_card_to_create_reaches_analyse(message):
    assert R._fast_path(message, prior=None) == "analyse"


@pytest.mark.parametrize("message", (
    "Le chiffre d'affaires est-il un bon indicateur ?",
    "Quel est le chiffre d'affaires du mois ?",
))
def test_the_word_alone_keeps_its_pole(message):
    assert R._fast_path(message, prior=None) != "analyse"
