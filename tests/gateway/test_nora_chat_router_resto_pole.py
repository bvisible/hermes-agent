# //// Neoffice — added file (no upstream equivalent): the restaurant pole and the routing defects measured on 05.11.
"""The restaurant is its own pole (`resto`), and five routing defects found by replaying 527 real requests (05.11).

Each case below was a real request routed to the wrong pole on the development instance:
« J'ai pris la bière » went to projet four times (the « work already done » rule), an invoice with an item sold by the
m² went to projet (the take-off rule), « …, oui, relance l'import » to compta (the dunning rule), « où en est mon import
d'articles ISI-… ? » to support, « qu'est-ce qui attend dans À traiter ? » to rh, « fais-la créer par le poste Ventes »
stayed on projet, and « tu te souviens du code … ? » went to projet although only Nora holds the memory.
"""

import pytest

from gateway import nora_chat_router as r
from gateway.kanban_watchers_notifier import _NEOFFICE_DOMAINS


def test_the_restaurant_is_a_pole_with_a_name_in_every_language():
    assert "resto" in r.POLES
    assert {lang: labels["resto"] for lang, labels in r.POLE_LABELS.items()} == {
        "fr": "Restaurant",
        "de": "Restaurant",
        "it": "Ristorante",
        "en": "Restaurant",
    }
    assert _NEOFFICE_DOMAINS["resto"] == r.POLE_LABELS["fr"]["resto"]


@pytest.mark.parametrize(
    "message",
    ["Quels plats me rapportent le plus ?", "Quels sont mes plats les plus rentables ?", "Qu'est-ce que je devrais revoir sur ma carte ?",
     "La rentabilité de ma carte", "Fais-moi une analyse de ma carte", "La marge de mes plats"],
)
def test_what_the_card_earns_is_the_restaurant_pole(message):
    assert r._keyword_pole(message) == "resto"


@pytest.mark.parametrize("message", ["Quelle est ma marge sur cette facture ?", "Quels sont mes articles les plus vendus ?", "Analyse de mes ventes"])
def test_the_margin_of_a_shop_is_not_the_card(message):
    assert r._keyword_pole(message) != "resto"


def test_the_shopping_list_page_belongs_to_the_restaurant_pole():
    assert r._DOCTYPE_POLES["Restaurant Shopping List"] == "resto"


@pytest.mark.parametrize(
    "message",
    ["Je fais ma liste de courses : il me faut des carottes", "Compose-moi un menu du soir à 38 francs",
     "Note la recette du risotto : pour quatre, 320 g de riz", "Qu'est-ce qui est de saison en novembre ?",
     "Mardi, quarante couverts : la soupe de courge, il faut acheter quoi ?",
     "Combien me coûte une portion de la soupe de courge ?"],
)
def test_what_the_restaurant_owner_says_goes_to_the_restaurant_pole(message):
    assert r._keyword_pole(message) == "resto"


def test_the_classifier_can_answer_the_restaurant_pole_and_knows_what_it_holds():
    prompt = r._CLASSIFIER_SYSTEM
    assert "recurrent, compta, projet, ventes, resto, support, rh, analyse, direct" in prompt
    resto = prompt[prompt.index("- resto :"): prompt.index("- support :")]
    for words in ("LISTE DE COURSES", "CARTE du restaurant", "MENUS", "RECETTES", "SAISON", "ACHETER", "j'ai pris la bière"):
        assert words in resto, words


@pytest.mark.parametrize("message", ["J'ai pris la bière.", "J'ai pris le pain", "j ai pris la farine"])
def test_a_restaurant_owner_ticking_his_list_is_not_work_done_on_a_job(message):
    assert r._keyword_pole(message) != "projet"


@pytest.mark.parametrize(
    "message",
    ["J'ai pris un mitigeur chez Dupont", "j'ai pris deux joints sur le chantier", "J'ai pris un siphon pour le chantier PROJ-0087"],
)
def test_a_part_taken_on_a_job_is_still_work_done_on_a_job(message):
    assert r._keyword_pole(message) == "projet"


def test_an_item_sold_by_the_square_metre_is_not_a_take_off():
    message = "Crée une facture brouillon pour Atelier Exemple SA avec 1 Plateau chêne massif (m²) et 1 Mitigeur thermostatique."
    assert r._keyword_pole(message) != "projet"


@pytest.mark.parametrize("message", ["La cuisine fait 12 m² au sol", "Combien de m2 de carrelage pour 3 x 4 ?"])
def test_a_surface_is_still_a_take_off(message):
    assert r._keyword_pole(message) == "projet"


@pytest.mark.parametrize(
    "message",
    ["Importe mes articles du fichier liste.csv : oui, relance l'import avec ces réglages",
     "Où en est mon import d'articles ISI-2026-17625 ?",
     "Montre-moi encore une fois ce que l'import ISI-2026-17625 va créer.",
     "Importe le catalogue du fournisseur"],
)
def test_an_item_import_goes_to_the_sales_pole(message):
    assert r._keyword_pole(message) == "ventes"


@pytest.mark.parametrize("message", ["Relance le client Dupont pour sa facture", "Fais une relance pour les factures échues"])
def test_a_payment_reminder_still_goes_to_accounting(message):
    assert r._keyword_pole(message) == "compta"


@pytest.mark.parametrize("message", ["Qu'est-ce qui attend dans À traiter ?", "Montre-moi l'onglet À traiter", "À traiter ?"])
def test_the_to_handle_list_is_the_jobs_pole(message):
    assert r._keyword_pole(message) == "projet"


def test_invoices_to_handle_are_not_the_jobs_list():
    assert r._keyword_pole("Montre-moi les factures à traiter") != "projet"


@pytest.mark.parametrize(
    "message, pole",
    [("Fais-la créer par le poste Ventes.", "ventes"), ("Transmets ça au service compta", "compta"),
     ("Demande à l'équipe chantier", "projet"), ("Passe ça au pôle restaurant", "resto")],
)
def test_a_pole_the_person_names_wins_over_the_thread(message, pole):
    assert r._fast_path(message, {"msg": "Crée une facture brouillon", "pole": "support"}) == pole


@pytest.mark.parametrize(
    "message, prior",
    [("Tu te souviens du code de mon projet Genève que je t'ai donné tout à l'heure ?", None),
     ("Retiens le numéro 5218 pour la suite de cette conversation, sans rien faire d'autre.", {"msg": "x", "pole": "compta"}),
     ("Quel numéro t'ai-je demandé de retenir tout à l'heure ?", {"msg": "x", "pole": "compta"})],
)
def test_the_memory_of_the_conversation_is_nora_s_own(message, prior):
    assert r._fast_path(message, prior) == "DIRECT"


def test_a_deduction_is_not_the_memory():
    assert r._fast_path("Retiens 10 % sur la facture FA-2026-00012", None) != "DIRECT"


def test_the_classifier_is_told_what_it_did_not_know():
    prompt = r._CLASSIFIER_SYSTEM
    for words in ("À traiter", "IMPORT d'articles", "numéros de série", "MÉMOIRE", "NOMME un pôle"):
        assert words in prompt, words


@pytest.mark.parametrize(
    "message",
    ["Fais une facture d'une borne wifi EAP 723 à Jules Exemple.", "Tu crées la facture, s'il te plaît ?", "Crée une facture pour Atelier Exemple SA",
     "Fais-moi une nouvelle facture pour une borne Wi-Fi", "crée une facture daniel exemple pour un epa730", "Modifie la facture FA-2026-02477 : il faut trois siphons",
     "Remets la RPLP sur le mitigeur de la facture FA-2026-02585."],
)
def test_writing_a_sales_invoice_is_the_sales_pole(message):
    assert r._keyword_pole(message) == "ventes"


@pytest.mark.parametrize(
    "message, pole",
    [("Crée une facture fournisseur pour Exemple SA", "compta"), ("Combien de factures impayées avons-nous ?", "compta"), ("Modifie la facture fournisseur 12", "compta")],
)
def test_a_supplier_invoice_and_reading_invoices_stay_with_accounting(message, pole):
    assert r._keyword_pole(message) == pole


@pytest.mark.parametrize(
    "message",
    ["Alors attends, c'est toujours pas juste, tu as écrit Daniel Moret, c'est M-O-R-E-T, Daniel Moret.", "Non, tu as écrit Dupond avec un d",
     "C'est faux, tu t'es trompé de client", "Ce n'est pas correct", "t’as écrit Moret, pas Morey"],
)
def test_a_correction_of_what_the_desk_wrote_stays_with_it(message):
    assert r._fast_path(message, {"msg": "x", "pole": "compta"}) == "compta"


def test_a_correction_with_no_desk_before_it_is_left_to_the_rest():
    assert r._fast_path("Tu as écrit Dupond avec un d", None) is None


@pytest.mark.parametrize(
    "message",
    ["Tu as écrit Dupond, et fais une facture à Martin pour le mitigeur ? Il y a trois autres choses à te demander sur ce client et sur sa commande de la semaine dernière et aussi sur son devis du mois passé qui attend.",
     "Combien de congés me reste-t-il ?"],
)
def test_a_long_message_or_a_new_question_is_not_a_correction(message):
    assert r._fast_path(message, {"msg": "x", "pole": "compta"}) != "compta"


@pytest.mark.parametrize(
    "message",
    ["Dans quel texte de l'AFC la RPLP est-elle citée pour un envoi à l'étranger, et est-ce encore valable ?", "Que dit l'AFC sur les repas d'affaires ?",
     "Que dit la LTVA sur les acomptes ?"],
)
def test_the_law_of_the_tax_is_accounting_and_its_wiki(message):
    assert r._keyword_pole(message) == "compta"


def test_a_note_named_by_its_number_is_support():
    assert r._keyword_pole("Que dit la note VN-2026-17468 ?") == "support"


ASKED = {"msg": "Chiffre le chantier", "pole": "projet", "asked": True}


@pytest.mark.parametrize(
    "message", ["on facture à l'heure", "à l'heure", "au forfait", "le devis, pas la facture", "crée-le sans e-mail", "celui de Nyon", "Fais-le sans adresse"]
)
def test_the_answer_to_a_question_stays_with_the_desk_that_asked(message):
    assert r._fast_path(message, ASKED) == "projet"


@pytest.mark.parametrize(
    "message",
    ["Fais une facture à Dupont", "Combien de congés me reste-t-il ?", "Quel est mon chiffre d'affaires", "Rappelle-moi demain à 9h de rappeler Dupont",
     "Tous les lundis, envoie-moi les factures impayées", "Fais-la créer par le poste Ventes."],
)
def test_a_new_request_leaves_the_desk_that_asked(message):
    assert r._fast_path(message, ASKED) != "projet"


def test_without_a_question_waiting_the_rules_decide_as_before():
    assert r._fast_path("le devis, pas la facture", {"msg": "x", "pole": "projet"}) == "ventes"


def test_the_board_says_which_desk_waits(monkeypatch, tmp_path):
    import sqlite3
    import time

    from hermes_cli import kanban_db_connect

    path = tmp_path / "kanban.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE tasks (assignee TEXT, status TEXT, block_kind TEXT, idempotency_key TEXT, created_at INTEGER)")
    now = int(time.time())
    conn.executemany(
        "INSERT INTO tasks VALUES (?,?,?,?,?)",
        [("rh", "blocked", "needs_input", "c1:1", now - 9000), ("projet", "blocked", "needs_input", "c1:2", now - 60),
         ("ventes", "blocked", "technical", "c1:3", now - 10), ("compta", "done", None, "c1:4", now), ("rh", "blocked", "needs_input", "c9:1", now)],
    )
    conn.commit()
    monkeypatch.setattr(kanban_db_connect, "connect", lambda **kw: sqlite3.connect(path))
    assert r._question_waiting("c1") == "projet"
    assert r._question_waiting("c2") is None
    assert r._question_waiting(None) is None
    conn.execute("INSERT INTO tasks VALUES ('resto', 'blocked', NULL, 'c3:1', ?)", (now,))  # a worker that said no kind asks too
    conn.execute("INSERT INTO tasks VALUES ('resto', 'blocked', 'technical', 'c4:1', ?)", (now,))
    conn.commit()
    assert r._question_waiting("c3") == "resto"
    assert r._question_waiting("c4") is None
    monkeypatch.setattr(kanban_db_connect, "connect", lambda **kw: 1 / 0)
    assert r._question_waiting("c1") is None
