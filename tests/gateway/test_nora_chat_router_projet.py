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


# //// Neoffice — the job pole's own questions must not depend on the model answering.
# //// Measured 17.09: the gateway gives the classifier EIGHT seconds and falls back when
# //// it does not reply — 33 times in the log, 14 that day. Each of these sentences used
# //// to match no rule at all, so each was one slow model call away from being answered
# //// by an agent holding no pole tool.
@pytest.mark.parametrize(
    "message",
    (
        "crée un chantier pour une rénovation de salle de bain",
        "ouvre un chantier chez le client",
        "quels chantiers demandent mon attention ?",
        "quels chantiers sont libres en ce moment ?",
        "quels chantiers est-ce que je dois terminer ?",
        "combien de chantiers en cours ?",
        "quelles natures de chantier est-ce qu'on gère ?",
        "c'est quoi ma journée aujourd'hui ?",
        "clôture ma journée",
        "montre-moi ma tournée",
        "est-ce que j'ai le temps de passer avant 16h ?",
        "j ai le temps de passer ?",
        "transforme cette note en ligne de travail",
        "demande l'avis d'un expert sur ce chantier",
    ),
)
def test_the_job_poles_own_questions_are_deterministic(message):
    assert _fast_path(message, None) == "projet"


@pytest.mark.parametrize(
    ("message", "pole"),
    (
        # The plural + question shape is the discriminator; these have neither.
        ("recrute un ouvrier pour le chantier", None),
        ("crée un client Jean Dupont", None),
        ("quels employés sont disponibles ?", None),
        # And the neighbours the job rules sit next to keep their own routes.
        ("quel est le chiffre d'affaires du mois ?", "compta"),
        ("fais un devis pour ce client", "ventes"),
        ("ajoute deux heures de congé", "rh"),
        ("envoie un mail au sujet du chantier PROJ-0094", "support"),
    ),
)
def test_the_job_question_rule_stays_narrow(message, pole):
    """None means « no rule » — the classifier still decides, which is correct here."""
    assert _fast_path(message, None) == pole


# //// Neoffice — a guard is tested FAILING, not only succeeding.
class _Boom(Exception):
    pass


def _exploding_llm(**_kwargs):
    raise _Boom("model busy")


def test_a_failed_classification_stays_on_the_prior_pole():
    """Falling to DIRECT hands the turn to an agent holding no pole tool.

    The prior pole was chosen by a classification that DID succeed, so staying
    there is strictly better than dropping the thread's tools mid-conversation.
    """
    from gateway.nora_chat_router import classify

    verdict = classify(
        "et pour ce client-là ?",
        call_llm_fn=_exploding_llm,
        main_runtime=None,
        prior={"msg": "liste mes factures en retard", "pole": "compta"},
    )
    assert verdict == "compta"


def test_a_failed_classification_with_no_prior_still_degrades_to_direct():
    """No prior means nothing better to fall back to — never drop the message."""
    from gateway.nora_chat_router import classify

    verdict = classify(
        "et pour ce client-là ?",
        call_llm_fn=_exploding_llm,
        main_runtime=None,
        prior=None,
    )
    assert verdict == "DIRECT"


def test_a_failed_classification_ignores_a_prior_that_is_not_a_pole():
    """DIRECT is not a pole: a prior of DIRECT must not be echoed back as one."""
    from gateway.nora_chat_router import classify

    verdict = classify(
        "et pour ce client-là ?",
        call_llm_fn=_exploding_llm,
        main_runtime=None,
        prior={"msg": "bonjour", "pole": "DIRECT"},
    )
    assert verdict == "DIRECT"


# //// Neoffice — « relance » means three different jobs depending on ONE noun beside it.
# //// The dunning rule matches \brelanc\w* on purpose — a payment reminder is phrased a
# //// dozen ways — and it sits above the quotation rules, so until 17.09 it swallowed
# //// both a sales follow-up and a chase on a job's quotation, landing them on compta:
# //// the pole that holds the dunning tools and not one gesture either of them needs.
@pytest.mark.parametrize(
    ("message", "pole"),
    (
        # a prospect can only be a sale
        ("relance ce prospect", "ventes"),
        ("relance les prospects de la semaine", "ventes"),
        ("ce lead, il faut le relancer", "ventes"),
        # a quotation ON A JOB is projet's — it holds frappe_job_quote
        ("relance le devis du chantier PROJ-0094", "projet"),
        ("relance l'offre de ce chantier", "projet"),
        # everything else is still a collection, which is what the broad rule is for
        ("relance la facture FA-0042", "compta"),
        ("envoie un rappel de paiement", "compta"),
        ("relance-le", "compta"),
        ("lettre de relance pour ce client", "compta"),
    ),
)
def test_a_follow_up_goes_where_its_noun_points(message, pole):
    assert _fast_path(message, None) == pole


def test_a_payment_reminder_about_a_job_is_still_a_collection():
    """The job rule needs a QUOTATION noun, not merely a job.

    « relance de paiement pour le chantier X » is money, and compta holds the
    dunning tools. Without this the job anchor alone would have stolen it, which
    is the mistake the three lookaheads exist to prevent.
    """
    assert _fast_path("relance de paiement pour le chantier PROJ-0094", None) == "compta"


# //// Neoffice — the guard that would have caught this on 16.09. `projet` was added to
# //// POLES that day and the classifier prompt never named it, so for four days the model
# //// was asked to pick a pole and could not answer this one: everything the deterministic
# //// rules did not catch went to a pole holding no job tool. Found 20.09 by another
# //// session reading the prompt, not by any test.
def test_every_pole_can_be_named_by_the_classifier():
    """Each pole in POLES is offered in the allowed tokens AND described.

    Two separate things, and both are needed: a token the model may not emit is
    unreachable, and a token with no domain line is a word the model has no
    reason to choose.
    """
    from gateway.nora_chat_router import POLES, _CLASSIFIER_SYSTEM

    premiere_ligne = _CLASSIFIER_SYSTEM.split("\n")[1] if "\n" in _CLASSIFIER_SYSTEM else _CLASSIFIER_SYSTEM
    jetons = _CLASSIFIER_SYSTEM.split("Tu réponds par UN SEUL mot parmi :", 1)
    assert len(jetons) == 2, "the allowed-token sentence moved; this guard must follow it"
    liste = jetons[1].split(".", 1)[0]
    for pole in POLES:
        assert pole in liste, f"{pole} is in POLES but the model may not answer it"
        assert f"- {pole} :" in _CLASSIFIER_SYSTEM, f"{pole} has no domain line in the prompt"


def test_the_job_domain_names_its_boundary_with_ventes():
    """The two compete on one word — « devis » — so the prompt says where it splits."""
    from gateway.nora_chat_router import _CLASSIFIER_SYSTEM

    assert "QUALIFIÉS PAR UN CHANTIER" in _CLASSIFIER_SYSTEM
    assert "SANS chantier reste" in _CLASSIFIER_SYSTEM


# //// Neoffice — the anchor injected on a job page (20.09). It used to be built inline
# //// inside route_chat, whole, for whatever pole the turn was routed to: compta, ventes,
# //// rh, support and analyse were all told to read `chantier-gestes-nora` — a skill that
# //// exists only under skills-poles/projet — and to pass `project` to seven tools that
# //// moved to `projet` on 16.09. Naming a tool the worker does not hold teaches a tool
# //// that does not exist, and the worker spends its turn finding out.
JOB = {"title": "Rénovation salle de bain", "customer": "Dupont"}


def test_every_pole_learns_which_job_the_user_is_looking_at():
    """The IDENTITY half is for everyone: « facture ce chantier » is a compta sentence."""
    from gateway.nora_chat_router import POLES, job_page_anchor

    for pole in POLES:
        ancre = job_page_anchor(pole, "Page context — the user is on building job", "PROJ-0087", JOB)
        assert ancre.startswith("[") and ancre.endswith("]"), f"{pole}: {ancre}"
        assert "PROJ-0087" in ancre, f"{pole} is not told which job"
        assert 'project="PROJ-0087"' in ancre, f"{pole} is not told what to pass"
        assert "Never guess another job." in ancre, pole


def test_only_the_job_pole_is_sent_to_the_job_skill():
    """`chantier-gestes-nora` lives under skills-poles/projet and nowhere else."""
    from gateway.nora_chat_router import POLES, job_page_anchor

    for pole in POLES:
        ancre = job_page_anchor(pole, "Page context — the user is on building job", "PROJ-0087", JOB)
        if pole == "projet":
            assert "chantier-gestes-nora" in ancre
            assert "frappe_job_add_lines" in ancre
        else:
            assert "chantier-gestes-nora" not in ancre, (
                f"{pole} is sent to a skill it does not have: {ancre}"
            )
            assert "frappe_job_" not in ancre, (
                f"{pole} is handed a job tool it no longer holds: {ancre}"
            )


def test_the_anchor_names_no_tool_by_hand():
    """A hand-kept list drifts; this one had, and lost the tool of the 16.09 incident.

    The rule in its place — « wherever one of your tools takes a `project` argument » —
    is read by the worker off its OWN schemas, so a tool added tomorrow is covered and
    the ones keyed on an activity stay correctly out.
    """
    from gateway.nora_chat_router import job_page_anchor

    ancre = job_page_anchor("projet", "The request names the building job", "PROJ-0087", JOB)
    assert "`project` argument" in ancre
    for jamais in ("frappe_job_status", "frappe_job_tasks", "frappe_job_book_visit",
                   "frappe_job_quote", "frappe_job_customer_said_yes", "frappe_job_invoice",
                   "frappe_job_take", "frappe_job_ask_expert"):
        assert jamais not in ancre, f"{jamais} is enumerated again — the list will drift"


def test_the_anchor_never_claims_a_page_the_user_was_not_on():
    """A lie in the body is a lie the worker repeats: the source phrase is carried in."""
    from gateway.nora_chat_router import job_page_anchor

    depuis_message = job_page_anchor("projet", "The request names the building job", "PROJ-0087")
    assert depuis_message.startswith("[The request names the building job PROJ-0087")
    assert "Page context" not in depuis_message


def test_a_job_without_title_or_customer_still_anchors():
    """The desk does not always send a title; the job number alone must still carry."""
    from gateway.nora_chat_router import job_page_anchor

    for vide in (None, {}, {"title": "", "customer": ""}):
        ancre = job_page_anchor("projet", "Page context — the user is on building job", "PROJ-0087", vide)
        assert "PROJ-0087" in ancre and " «  » " not in ancre, ancre


# //// Neoffice — added 20.09. A capability question ("tu peux … ?") is settled in
# //// _fast_path BEFORE the keyword rules, so every question about a chantier reaches
# //// _CAPABILITY_SYSTEM and nothing else. That prompt listed the domain by hand and
# //// stopped at "quotes, invoices, clients, articles, payment reminders, emails, HR
# //// and charts" — the pole `projet` and its seventeen write tools had been live for
# //// four days. Measured against the model that day: asked to open a chantier it
# //// invented an intake (montant estimé, devis associé) that frappe_job_create does
# //// not take, and asked for a métré it requested the quantities and unit prices that
# //// frappe_measure exists to COMPUTE.
def test_a_question_about_a_chantier_is_a_capability_question():
    """Whatever the domain, "tu peux …" lands on this one prompt — so it must know it."""
    from gateway.nora_chat_router import _CAPABILITY_RE

    for q in (
        "Tu peux ouvrir un chantier ?",
        "Peux-tu me faire un métré ?",
        "Tu peux planifier une visite chez un client ?",
        "Est-ce que tu peux gérer un contrat d'entretien ?",
    ):
        assert _CAPABILITY_RE.match(q), f"no longer a capability question: {q}"


def test_the_capability_prompt_names_the_job_domain():
    """The pole exists; a prompt that has never heard of it makes the model improvise."""
    from gateway.nora_chat_router import _CAPABILITY_SYSTEM

    minuscule = _CAPABILITY_SYSTEM.lower()
    for mot in ("building-job", "visits", "take-off", "costing", "maintenance contracts"):
        assert mot in minuscule, f"the capability domain does not name {mot}"


def test_the_capability_prompt_does_not_promise_out_of_domain():
    """« say yes » alone is a promise; the domain has to gate it."""
    from gateway.nora_chat_router import _CAPABILITY_SYSTEM

    assert "If the request is in that domain, say yes" in _CAPABILITY_SYSTEM
    assert "If it is NOT in that domain" in _CAPABILITY_SYSTEM
    assert "never promise it" in _CAPABILITY_SYSTEM


def test_the_capability_prompt_forbids_asking_for_what_is_computed():
    """The métré answer asked the user for the quantity the tool returns."""
    from gateway.nora_chat_router import _CAPABILITY_SYSTEM

    assert "never for something the system works out by itself" in _CAPABILITY_SYSTEM
    assert "do not invent data or fields" in _CAPABILITY_SYSTEM


# //// Neoffice — added 20.09. _BUSINESS_RE is the ANTI-gate of the small-talk light
# //// path: a message carrying a greeting AND no business word is answered with one
# //// warm sentence and never reaches the agent. The list was written before `projet`
# //// existed, so it protected « Bonjour, ou en est ma facture ? » and not « Bonjour,
# //// on en est ou sur le chantier ? ». Measured that day: five job sentences out of six
# //// passed straight through. The keyword rules catch most of them first, but this gate
# //// exists for when they do not and the classifier falls back to DIRECT — which is
# //// what an outage does, all day (14.09).
JOB_SENTENCES_WITH_A_GREETING = (
    "Bonjour, on en est où sur le chantier ?",
    "Salut, la visite de mardi tient toujours ?",
    "Bonjour, il me faut un métré pour le séjour.",
    "Merci, et l'atelier a fini la réparation ?",
    "Bonsoir, j'ai posé deux heures sur le chantier.",
    "Bonjour, le contrat d'entretien arrive à échéance ?",
    "Bonjour, l'intervention de jeudi est planifiée ?",
)


def test_a_greeting_in_front_of_a_job_question_is_not_small_talk():
    """The failure is asymmetric: a false positive costs latency, a false negative answers
    a real question with « Bonjour ! Comment puis-je vous aider ? »."""
    from gateway.nora_chat_router import _BUSINESS_RE

    for phrase in JOB_SENTENCES_WITH_A_GREETING:
        assert _BUSINESS_RE.search(phrase), (
            "would take the small-talk light path: " + phrase
        )


def test_the_job_gate_is_as_strong_as_the_invoice_gate():
    """The control: the same sentence about an invoice was always protected."""
    from gateway.nora_chat_router import _BUSINESS_RE

    assert _BUSINESS_RE.search("Bonjour, où en est ma facture ?")
    assert _BUSINESS_RE.search("Bonjour, où en est mon chantier ?")


def test_plain_small_talk_still_takes_the_light_path():
    """Widening the anti-gate must not cost a greeting its one-sentence answer."""
    from gateway.nora_chat_router import _BUSINESS_RE, _SMALLTALK_RE

    for phrase in ("Bonjour", "Bonjour Nora", "Merci !", "Bonne journée", "Salut, ça va ?",
                   "Bonsoir", "Au revoir", "Comment vas-tu ?"):
        assert _SMALLTALK_RE.search(phrase), phrase
        assert not _BUSINESS_RE.search(phrase), (
            "a plain greeting now pays the full agent: " + phrase
        )
