# //// Neoffice — added file (no upstream equivalent): recruitment reaches the rh pole (07.10, maintenance#1294).
"""Recruitment is rh's: the applications hrms receives on the job page, their reading, the job adverts.

Before the rule, « Relance la lecture de cette candidature » reached compta (the dunning rule), « crée une offre
d'emploi pour un chef de chantier » and « fixe un rendez-vous avec la candidate » reached projet (the job rules run
before the table), and « l'offre de chef de projet » reached projet (the quotation-of-a-job rule). « offre » alone
stays a quotation.
"""
import pytest

from gateway.nora_chat_router import _DOCTYPE_POLES, _fast_path, _page_document_pole, _rh_relance_stays


@pytest.mark.parametrize("message", (
    # the sentences the hrms session wrote for the feature
    "Quelles candidatures avons-nous reçues cette semaine ?",
    "Montre-moi les candidatures pour le poste de comptable, les mieux notées d'abord",
    "Résume-moi la candidature de Marie Dubois",
    "Que manque-t-il dans son CV ?",
    "Quelles questions poser à Marie Dubois en entretien ?",
    "Propose des critères pour l'offre de comptable",
    "Quelles offres sont publiées sur le site ?",
    "Retire l'offre de comptable du site",
    "Mets en ligne l'offre de magasinier",
    "Qui a postulé cette semaine ?",
    "Prépare des questions d'entretien pour le poste de vendeur",
    "Combien de postes à pourvoir avons-nous ?",
    # German, Italian, English
    "Welche Bewerbungen haben wir diese Woche erhalten?",
    "Zeig mir die offenen Stellen",
    "Quali candidature abbiamo ricevuto questa settimana?",
    "Riassumi il candidato Rossi",
    "Which job applications came in this week?",
    "Show me the applicants for the accountant job opening",
))
def test_a_recruitment_question_reaches_rh(message):
    assert _fast_path(message, prior=None) == "rh"


@pytest.mark.parametrize("message, was", (
    ("Relance la lecture de cette candidature", "compta"),
    ("Crée une offre d'emploi pour un chef de chantier", "projet"),
    ("Fixe un rendez-vous avec la candidate jeudi", "projet"),
    ("Propose des critères pour l'offre de chef de projet", "projet"),
    ("Valide l'offre d'emploi de comptable", "ventes"),
))
def test_what_another_rule_used_to_take_now_reaches_rh(message, was):
    assert _fast_path(message, prior=None) == "rh", f"was {was}"


@pytest.mark.parametrize("message", (
    "Fais une offre à Jules Dupondss pour un TP-Link EAP723",
    "Fais une offre pour la refonte du site de Martin",
    "Mets l'offre spéciale du mois sur le site",
    "Publie l'offre du jour sur le site",
    "Prépare le dossier de candidature pour l'appel d'offres de la commune",
    "Le moteur fait 150 CV",
    "La machine est en entretien jusqu'à lundi",
    "Quelles questions sur l'entretien de la chaudière ?",
    "Retire l'offre faite à Martin",
))
def test_a_quotation_a_tender_or_a_shop_offer_is_not_recruitment(message):
    assert _fast_path(message, prior=None) != "rh"


def test_sending_a_mail_to_a_candidate_still_reaches_the_mail_tools():
    assert _fast_path("Envoie un mail à la candidate pour fixer l'entretien", prior=None) == "support"
    assert _fast_path("Send an email to the applicant", prior=None) != "rh"


def test_a_chart_of_applications_reaches_analyse():
    assert _fast_path("Fais un graphique des candidatures par mois", prior=None) == "analyse"


@pytest.mark.parametrize("message, expected", (
    ("Relance le devis de Martin", "ventes"),
    ("Est-ce qu'il y a des rappels à faire ?", "compta"),
    ("Recrute un ouvrier pour le chantier", "rh"),
))
def test_the_neighbouring_rules_are_unchanged(message, expected):
    assert _fast_path(message, prior=None) == expected


def test_a_follow_up_about_applications_leaves_the_sales_pole():
    assert _fast_path("ok, et les candidatures ?", prior={"pole": "ventes"}) == "rh"


@pytest.mark.parametrize("message", ("oui, relance-la", "Oui relance la lecture", "ok relance"))
def test_a_yes_to_reading_an_application_again_stays_with_rh(message):
    assert _fast_path(message, prior={"pole": "rh"}) == "rh"


def test_a_yes_that_names_a_collection_still_reaches_compta():
    assert _fast_path("oui, relance les clients", prior={"pole": "rh"}) == "compta"
    assert _fast_path("ok relance la facture de Martin", prior={"pole": "rh"}) == "compta"


def test_only_an_rh_offer_keeps_its_relance():
    assert _rh_relance_stays("oui, relance-la", "rh", "compta")
    assert not _rh_relance_stays("oui, relance-la", "ventes", "compta")
    assert not _rh_relance_stays("oui, relance-la", "rh", "support")


@pytest.mark.parametrize("doctype", ("Job Applicant", "Job Opening", "Job Offer", "Interview"))
def test_the_page_of_an_application_or_an_opening_belongs_to_rh(doctype):
    assert _DOCTYPE_POLES[doctype] == "rh"
    assert _page_document_pole("Que manque-t-il dans ce dossier ?", doctype) == "rh"
# //// END Neoffice ////
