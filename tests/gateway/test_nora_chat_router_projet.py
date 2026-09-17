"""The `projet` pole owns building jobs — routing regressions.

The fourteen building-job writes (frappe_job_book_visit, frappe_job_quick,
frappe_job_record_work, frappe_job_quote…) moved out of `ventes` into `projet`
on 16.09. Every router rule that used to send job work to `ventes` had to move
with them: routed to a pole that no longer holds a single one of those tools,
the worker answers with the guard's English text instead of doing the work.

The quotation case is the one that failed in production. On 15.09, « compose le
devis de ce chantier » reached `ventes`, whose generic quotation tool produced a
ONE-LINE document — a single generic service line named after the project —
while the job's own costing held nothing at all.
"""

import pytest

from gateway.nora_chat_router import POLES, POLE_LABELS, _fast_path


@pytest.mark.parametrize(
    "message",
    (
        "compose le devis de ce chantier",
        "fais le devis du projet PROJ-0001",
        "devis pour l'intervention de mardi",
        "sur ce chantier, prépare une offre",
    ),
)
def test_a_quotation_qualified_by_a_job_routes_to_projet(message):
    assert _fast_path(message, None) == "projet"


@pytest.mark.parametrize(
    "message",
    (
        "fais un devis pour ce client",
        "transforme le devis DEV-0042 en facture",
        "combien de commandes client ce mois ?",
    ),
)
def test_an_unqualified_sales_quotation_stays_with_ventes(message):
    """The job rule must not swallow the sales quotation it sits in front of."""
    assert _fast_path(message, None) == "ventes"


@pytest.mark.parametrize(
    "message",
    (
        "je passe chez le client demain à 14h",
        "j'ai été sur place, deux heures",
        "j'ai pris un mitigeur sur le chantier",
    ),
)
def test_booking_and_reporting_work_route_to_projet(message):
    """Both fast paths moved with frappe_job_book_visit / frappe_job_record_work."""
    assert _fast_path(message, None) == "projet"


@pytest.mark.parametrize(
    "message",
    (
        "Note une heure de travail sur le chantier PROJ-0001",
        "enregistre 2h sur PROJ-0001",
        "pointe trois heures sur l'intervention de mardi",
        "ajoute du matériel sur le chantier",
    ),
)
def test_a_job_order_routes_to_projet(message):
    """The two other job rules only know the first person. An ORDER fell through
    to the keyword rules, where « heures » is an HR word: it reached RH, which
    holds no job tool, and the guardrail answered the customer in English."""
    assert _fast_path(message, None) == "projet"


@pytest.mark.parametrize(
    ("message", "pole"),
    (
        ("ajoute deux heures de congé", "rh"),
        ("enregistre le paiement de la facture", "compta"),
    ),
)
def test_an_order_without_a_job_anchor_stays_with_its_own_pole(message, pole):
    """The job-order rule is anchored on the job on purpose: an imperative alone
    would steal leave hours from RH — the very mistake it exists to undo."""
    assert _fast_path(message, None) == pole


def test_projet_is_a_known_pole_with_a_label_in_every_language():
    """A pole the router can return but cannot name would reach the user unnamed."""
    assert "projet" in POLES
    for lang, labels in POLE_LABELS.items():
        assert "projet" in labels, f"no label for projet in {lang}"
        assert labels["projet"], f"empty label for projet in {lang}"


def test_every_pole_the_router_knows_can_be_named():
    """The guard that would have caught the label gap: POLES and POLE_LABELS
    must agree, in every language, for every pole — not just the new one."""
    for lang, labels in POLE_LABELS.items():
        assert set(labels) == set(POLES), f"{lang}: {set(POLES) ^ set(labels)}"


# //// Neoffice — a MEASUREMENT reaches `projet`, the only pole holding frappe_measure.
# //// The first message below is the one measured on 17.09: it matched no rule, fell
# //// through to the LLM classifier, came back DIRECT, and was answered by the gateway
# //// agent — which holds not one pole tool, so it improvised the arithmetic in prose.
@pytest.mark.parametrize(
    "message",
    (
        "Sur le chantier, j ai trois murs identiques de 4.20 m de long sur 2.50 m de "
        "haut, et il faut deduire une porte de 0.90 m sur 2.05 m. Quelle surface a "
        "peindre au total ?",
        "fais-moi le métré de la pièce",
        "quel est le métré du sol ?",
        "calcule la surface : 4,20 x 2,50",
        "il me faut le cubage de la dalle",
        "combien de m2 pour ce mur ?",
        "ça fait combien de mètres carrés ?",
        "quelle superficie pour 12 x 3 ?",
    ),
)
def test_a_measurement_routes_to_projet(message):
    assert _fast_path(message, None) == "projet"


def test_the_decimal_point_does_not_cut_the_sentence():
    """The gap is [\\s\\S], not [^.!?].

    A dimension carries its own full stop — « 4.20 » — and the measurement noun
    usually sits in the NEXT sentence: « voici les cotes. Quelle surface ? ».
    A sentence-bounded gap can span neither, which is exactly how the first
    version of this rule failed the one message it was written for.
    """
    assert _fast_path("Le mur fait 4.20 m sur 2.50 m. Quelle surface ?", None) == "projet"


@pytest.mark.parametrize(
    ("message", "pole"),
    (
        ("quel est le chiffre d'affaires du mois ?", "compta"),
        ("fais un devis pour ce client", "ventes"),
        ("combien de congés me reste-t-il ?", "rh"),
    ),
)
def test_the_measurement_rule_does_not_steal_its_neighbours(message, pole):
    """It sits above the `devis` and `chiffre d affaires` rules — so it must be narrow."""
    assert _fast_path(message, None) == pole


def test_a_measurement_noun_without_figures_stays_unclaimed():
    """Half B needs a dimension arithmetic: a noun alone is not a métré."""
    assert _fast_path("la surface de vente du magasin est trop petite", None) != "projet"


# //// Neoffice — the field types this one-handed, on site. The rule already tolerates
# //// a missing apostrophe (« j ai »); a missing accent is the same sloppiness and must
# //// not route differently — unaccented, the sentence reached no rule and « heures »
# //// made it an HR matter.
@pytest.mark.parametrize(
    "message",
    (
        "j'ai été sur place, deux heures",
        "j ai ete sur place, deux heures",
        "jai bosse sur le chantier ce matin",
        "j'ai été sur le chantier, trois heures",
    ),
)
def test_a_work_report_survives_a_missing_accent_or_apostrophe(message):
    assert _fast_path(message, None) == "projet"


def test_a_leave_request_is_still_not_a_work_report():
    """The widened rule must not reach across into RH, which owns « congé »."""
    assert _fast_path("ajoute deux heures de congé", None) == "rh"
    assert _fast_path("ajoute deux heures de conge", None) == "rh"
