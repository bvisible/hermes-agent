# //// Neoffice — added file (no upstream equivalent): the record of a prospect is the sales pole's (#2674).
"""On the hub, 13.11, two requests on a prospect left ventes, the only pole with the lead tool: « Complète la fiche du
prospect … (CRM-LEAD-…) : … rendez-vous … devis » went to projet (the visit rule), « Complète le prospect … dans notre
CRM : … relances de factures impayées » to compta (the dunning rule); both answered that the prospect did not exist.
A narrow rule (_LEAD_RECORD_RE: a verb said to the record itself, in the imperative or the infinitive, then the
prospect, the lead or its « fiche ») skips the job rules, as recruitment does, and the table still decides: a chart
stays analyse's. Its number, « du CRM » and a participle decide nothing, so a prospect who is only the addressee of
something else keeps its rule. Every phrase of the independent review that must keep its pole is held here, with the
pole the router gave before. Replayed by the rules alone on the 481 real requests of the reference set, two change:
the two « Crée un prospect … », from the classifier to ventes, their pole in the set.
"""
import pytest

from gateway import nora_chat_router as R


@pytest.mark.parametrize("msg", [
    "Complète la fiche du prospect Exemple Sàrl (CRM-LEAD-2026-00012) : il tient ses rendez-vous sur un tableur et "
    "perd des clients à cause des devis tardifs",
    "Complète le prospect Exemple Sàrl dans notre CRM : ils ont aussi un souci de relances de factures impayées",
    "Ajoute ce prospect à notre CRM : Garage Exemple, 6 personnes",
    "Mets à jour le lead CRM-LEAD-2026-00031 : il a changé de numéro",
    "Crée un prospect pour le Garage Exemple, 6 personnes",
    "Complète la fiche prospect du Garage Exemple : ils sont 6",
])
def test_the_record_of_a_prospect_goes_to_ventes(msg):
    assert R._keyword_pole(msg) == "ventes"
    assert R._fast_path(msg, None) == "ventes"


@pytest.mark.parametrize("msg, pole", [
    # its number, « du CRM », a participle: the prospect is only the addressee
    ("Crée un chantier pour le prospect CRM-LEAD-2026-00012", "projet"),
    ("Fixe un rendez-vous avec le prospect CRM-LEAD-2026-00031 jeudi à 10h", "projet"),
    ("Envoie un e-mail aux prospects du CRM pour annoncer notre journée portes ouvertes", "support"),
    ("Fixe un rendez-vous avec le prospect Dupont, et note-le dans le CRM", "projet"),
    ("J'ai créé un prospect hier : envoie-lui un e-mail de bienvenue", "support"),
    ("J'ai ajouté un lead, fixe-lui un rendez-vous demain", "projet"),
    ("envoie un e-mail au prospect Dupont pour mettre à jour sa fiche dans le CRM", "support"),
    ("note sur la fiche du prospect que le rendez-vous est déplacé", "projet"),
    ("relance le devis du chantier du prospect CRM-LEAD-2026-00012", "projet"),
    ("Envoie un e-mail à ce prospect", "support"),
    ("Fixe un rendez-vous avec le prospect Exemple demain à 10h", "projet"),
    ("Crée un chantier pour le prospect Exemple", "projet"),
    ("Relance ce prospect", "ventes"),
    ("Recrute un chef de projet (lead) pour la Gruyère", "rh"),
    ("Qui est le lead du projet PROJ-0087 ?", "projet"),
    # a chart stays analyse's, with or without the record
    ("Fais un graphique des prospects du CRM par mois", "analyse"),
    ("Tableau de bord des leads de notre CRM", "analyse"),
    ("Graphique des fiches prospects créées ce trimestre", "analyse"),
    ("Combien de prospects avons-nous ajoutés ce mois ? Fais-moi un graphique", "analyse"),
    ("Complète la fiche du prospect Exemple et fais-moi un graphique de ses achats", "analyse"),
    ("Crée un prospect et montre-moi un tableau de bord des leads", "analyse"),
])
def test_every_other_phrase_keeps_its_pole(msg, pole):
    assert R._fast_path(msg, None) == pole


def test_the_table_holds_it_so_the_go_ahead_guard_sees_it():
    assert any(rx is R._LEAD_RECORD_RE and pole == "ventes" for rx, pole in R._FAST_PATH_RULES)
