"""Deterministic NORA chat pre-router (gateway-side).

ROOT CAUSE THIS FIXES (proven from agent.log on 2026-06-03): the Qwen orchestrator
sometimes emits the routing acknowledgment as plain TEXT and then STOPS, WITHOUT
calling ``kanban_create`` — so no task is created, no worker spawns, no answer is
ever delivered, and the desk poll times out (the "empty promise"). Prompt-level
``TOOL_USE_ENFORCEMENT`` (already active for qwen models) is demonstrably
insufficient: it is guidance, not a hard guarantee.

This router takes the LLM out of the routing DECISION and out of the task-creation
ACTION. A one-shot classifier (the same ``call_llm`` helper ``generate_title`` uses)
picks a business pole; we then create the kanban task IN CODE — exactly the call the
``kanban_create`` tool makes, so the dispatcher spawns the specialist worker — and
deliver a fixed French acknowledgment. The model can no longer "forget" to create the
task, because creating it is no longer the model's job.

Anything the classifier marks ``DIRECT`` (greetings, small talk, meta questions, a
one-sentence answer Nora can give without business data) — and any classifier failure
— falls back to the normal agent dispatch, so the direct-answer path is unchanged
(zero regression). The router is dependency-injected (call_llm, model runtime, deliver
function are passed in) so it is unit-testable and the gateway hook stays a thin call.

User-facing strings are French (Swiss business audience); code/comments are English.
"""

# //// Neoffice — ENTIRE MODULE is a NORA addition (no upstream equivalent). Merged 2026-06-06
# from the richer osiris-poc router (RECURRENT class, keyword fast-path, conversation-aware
# routing, follow-up body, ack-via-desk) + the fork's stable-dir-workspace. Deterministic chat
# pre-router: classify (1 word) then CREATE the kanban task IN CODE — never rely on the LLM to
# call kanban_create (the "empty promise" timeout). See hermes-poc/CLAUDE.md. ////
from __future__ import annotations

import logging

from gateway.neoffice_scope import with_launch_profile_secrets  # //// Neoffice — see route_chat_message
import re
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# Business poles NORA routes to. Mirrors the orchestrator SOUL roster (the "Pôle"
# column) — keep in sync with configs/SOUL.md so deterministic routing matches what
# the orchestrator would have chosen on its good days.
POLES = ("compta", "ventes", "support", "rh", "analyse", "projet")


# //// Neoffice — added function (no upstream equivalent), extracted 20.09. It was
# //// fifteen lines built inline inside route_chat, which is why nothing tested it and
# //// why it could carry two defects for four days. Pure: pole in, anchor out.
def job_page_anchor(pole: str, source: str, project: str, job: dict | None = None) -> str:
    """The anchor that tells a worker WHICH building job the user is looking at.

    Two halves, and they do not have the same audience:

    * the job's IDENTITY — every pole needs it. A user on a job page who asks compta
      « facture ce chantier » means THIS one, and compta is right to know which.
    * the job's GESTURES — the `chantier-gestes-nora` skill and the item-search rule —
      belong to `projet` and to nothing else. That skill lives under
      skills-poles/projet; telling any other pole to `skill_view` it sends the worker
      after something that is not there, and it spends a turn finding out.

    The tool list that used to sit here is gone on purpose. It named seven tools and
    had drifted: the fourteen job writes moved to `projet` on 16.09, so on any other
    pole those names were tools the worker no longer held — and `frappe_job_record_work`,
    the tool of the 16.09 incident, was never in the list at all, nor were nine others.
    A list kept by hand drifts again at the next tool. The RULE does not: a worker reads
    which of ITS OWN tools take a `project` off the schemas it already has, and the ones
    keyed on an activity (frappe_job_take, frappe_job_ask_expert) stay correctly out.
    """
    job = job if isinstance(job, dict) else {}
    ancre = f"[{source} {project}"
    if job.get("title"):
        ancre += f" « {str(job.get('title'))[:120]} »"
    if job.get("customer"):
        ancre += f" (customer: {job.get('customer')})"
    ancre += (
        ". « Ce chantier » / « ce projet » means THIS job: wherever one of your tools "
        f'takes a `project` argument, pass project="{project}". Never guess another job.'
    )
    if pole == "projet":
        ancre += (
            " FIRST read your skill chantier-gestes-nora (skill_view); do not search "
            "items yourself — hand the names to frappe_job_add_lines and, on `problems`, "
            "ask the user one question."
        )
    return ancre + "]"
# //// END Neoffice ////


# Pole → user-facing label, PER LANGUAGE. NORA names the business "desk" to the user;
# the internal key (compta/…) is never leaked. French is canonical (Swiss-FR audience)
# and the default; the other languages mirror it for the multilingual chat path.
# //// Neoffice — multilingual desk labels + ack templates (was French-only) ////
POLE_LABELS = {
    "fr": {"compta": "Comptabilité", "ventes": "Ventes", "support": "Support", "rh": "Ressources Humaines", "analyse": "Analyse", "projet": "Projets"},
    "de": {"compta": "Buchhaltung", "ventes": "Verkauf", "support": "Support", "rh": "Personalwesen", "analyse": "Analyse", "projet": "Projekte"},
    "it": {"compta": "Contabilità", "ventes": "Vendite", "support": "Supporto", "rh": "Risorse Umane", "analyse": "Analisi", "projet": "Progetti"},
    "en": {"compta": "Accounting", "ventes": "Sales", "support": "Support", "rh": "Human Resources", "analyse": "Analytics", "projet": "Projects"},
}
# Backwards-compat alias (the French label map, still referenced by name elsewhere).
POLE_LABELS_FR = POLE_LABELS["fr"]

# Immediate-ack templates per language ({label} = the localized desk name).
ACK_TEMPLATES = {
    "fr": "Je transmets votre demande à votre pôle {label}, je reviens vers vous très vite.",
    "de": "Ich leite Ihre Anfrage an {label} weiter — ich melde mich gleich.",
    "it": "Inoltro la sua richiesta al reparto {label}, torno subito da lei.",
    "en": "I'm passing this to your {label} desk — I'll be right back with you.",
}


def _norm_lang(language: Optional[str]) -> str:
    """Normalize a Frappe code ('fr', 'fr-CH', 'de'…) to a supported ack language; default 'fr'."""
    code = (language or "").split("-")[0].strip().lower()
    return code if code in ACK_TEMPLATES else "fr"
# //// END Neoffice ////

# Per-conversation last route (in-memory, gateway-process-scoped). Lets the classifier
# resolve follow-ups IN CONTEXT: "Ceux de ce client" after "Combien de devis ouverts ?"
# (→ ventes) is the SAME ventes query filtered by a CLIENT — not an RH question about a
# person. Without this, each message is classified blind and a name-only follow-up gets
# mis-routed (verified 2026-06-03: "Ceux de ce client" → rh). Resets on gateway restart
# (the first follow-up after a restart degrades to context-free classify — acceptable).
_LAST_ROUTE: dict = {}
_LAST_ROUTE_MAX = 1000  # bound the dict; cleared wholesale when exceeded (cheap, rare)

# //// Neoffice — rolling per-conversation history of recent turns (the "film").
# A ROUTED worker runs as a FRESH, session-less kanban task: it sees ONLY the current
# message, so a MULTI-STEP request loses its thread (build a subscription → give the
# client, then the product, then the plan, across turns → by the last turn the worker
# no longer knows the client/goal from two turns earlier). Observed 2026-06-18: the
# worker created the client, then forgot it and the goal, then mis-parsed "Parfait" as a
# client name. We carry the recent turns into the worker task body so it can pick up
# the build and continue to completion. In-memory, gateway-process-scoped, bounded like
# _LAST_ROUTE. (Long-term mem0 memory is per-user and unaffected — a separate mechanism;
# this only restores the short-term conversation thread for routed workers.)
#
# The film is BILATERAL: entries are prefixed "User:" / "NORA:". NORA's own replies
# (fast-answer hits, delivered worker results) are recorded via note_nora_reply() —
# without them a follow-up like "ok crée un rappel" right after NORA listed the due
# invoices reaches the worker with no way to know WHICH invoices the user means
# (observed 2026-07-08: the reply "je ne sais pas quel rappel…"). grep "//// Neoffice".
_CONV_HISTORY: dict = {}
_CONV_HISTORY_TURNS = 10  # recent film entries carried to the worker (user + NORA lines)


# //// Neoffice — a bare yes answers a PROPOSAL, never a question. With NORA's question
# //// missing from the film, « Oui, vas-y » after « Pourriez-vous me donner le nom exact
# //// du client ? » had the worker pick a customer and change its address, and after « sur
# //// quel document ? » submit a quotation (capability bench, 2026-09-24). A CHOICE is a
# //// question too: after « remplacer l'e-mail principal ou ajouter un contact
# //// supplémentaire ? », a second « Oui, vas-y. » had the worker replace the e-mail, which
# //// put another person's address on the existing contact (capability bench, 2026-09-26).
_BARE_YES_RULE = (
    "A bare « oui / vas-y / ok / d'accord » CONFIRMS only a change NORA PROPOSED in its last "
    "line (shown to the user before being made). If NORA's last line was a QUESTION — which "
    "customer, which document, which article, a missing figure — « oui » does not answer it: "
    "ask that question again and change NOTHING. If it offered a CHOICE between options "
    "(« remplacer … ou ajouter … ? »), « oui » picks none of them: ask again, naming each "
    "option, and never take the one that overwrites or deletes existing data."
)
# //// END Neoffice ////


def note_nora_reply(conversation_id: Optional[str], text: Optional[str]) -> None:
    """Record NORA's own delivered reply into the conversation film.

    Called on the fast-answer path (below) and by the kanban notifier after a
    worker result is delivered to the chat (gateway/kanban_watchers.py) — both
    run in the gateway process, so they share this module's dict. Whitespace is
    collapsed and the entry truncated: the film feeds an LLM prompt, not a log.
    """
    clean = " ".join((text or "").split())
    if not (conversation_id and clean):
        return
    if len(_CONV_HISTORY) > _LAST_ROUTE_MAX:
        _CONV_HISTORY.clear()
    film = _CONV_HISTORY.setdefault(conversation_id, [])
    entry = "NORA: " + clean[:400]
    # //// Neoffice — the same reply can be recorded twice (the desk delivery, then the
    # //// notifier after it); one line in the film.
    if film and film[-1] == entry:
        return
    # //// END Neoffice ////
    film.append(entry)
    del film[:-_CONV_HISTORY_TURNS]
# //// END Neoffice ////

# //// Neoffice — briefing CTA → reply continuity (cross-process bridge).
# A WhatsApp morning briefing is pushed from Frappe straight to the WhatsApp router,
# OUTSIDE this gateway, so its call-to-action question ("voulez-vous que je prépare les
# rappels ?") never entered _LAST_ROUTE/_CONV_HISTORY. When the user replies "oui", we'd
# classify it blind → DIRECT → generic greeting, with NO continuity (reported 2026-06-19).
# The briefing records a "pending offer" {pole, action_en, question_fr, ts} keyed by phone
# in a shared file (Frappe + this gateway run as the SAME local user); we read it here on
# the FIRST inbound from that phone and continue the thread on the offer's pole. JSON
# contract mirrors nora/tasks/briefing_followup.py — keep in sync. grep "//// Neoffice".
import json as _json_off
import os as _os_off
import time as _time_off

_PENDING_OFFERS = _os_off.path.join(
    _os_off.path.expanduser("~"), ".hermes-nora-work", "pending_offers.json"
)
_OFFER_TTL_SECONDS = 6 * 3600
# An affirmative / "go ahead" reply → carry out the offered action. A name-only or new
# question is NOT here (it falls through to normal classification). Negative → drop it.
_AFFIRM_RE = re.compile(
    r"\b(oui|ouais|ok|okay|d'accord|daccord|volontiers|carr[ée]ment|vas[- ]?y|allez[- ]?y|"
    r"go|bien s[ûu]r|je veux bien|avec plaisir|les deux|pr[ée]pare|montre|envoie|fais[- ]?le|"
    r"parfait)\b",
    re.IGNORECASE,
)
_NEGATE_RE = re.compile(
    r"\b(non|nope|pas maintenant|plus tard|laisse tomber|pas besoin|une autre fois)\b",
    re.IGNORECASE,
)


def _offer_phone_key(phone: Optional[str]) -> str:
    return "".join(c for c in (phone or "") if c.isdigit())


def _read_pending_offer(phone: Optional[str]) -> Optional[dict]:
    """Return a fresh pending offer for this phone, or None. Never raises."""
    key = _offer_phone_key(phone)
    if not key:
        return None
    try:
        with open(_PENDING_OFFERS, encoding="utf-8") as fh:
            data = _json_off.load(fh) or {}
    except Exception:
        return None
    off = data.get(key)
    if not isinstance(off, dict) or not off.get("pole"):
        return None
    if (_time_off.time() - float(off.get("ts", 0))) > _OFFER_TTL_SECONDS:
        return None
    return off


def _consume_pending_offer(phone: Optional[str]) -> None:
    """Delete the offer for this phone so it fires once. Never raises."""
    key = _offer_phone_key(phone)
    if not key:
        return
    try:
        with open(_PENDING_OFFERS, encoding="utf-8") as fh:
            data = _json_off.load(fh) or {}
        if key in data:
            del data[key]
            tmp = _PENDING_OFFERS + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                _json_off.dump(data, fh)
            _os_off.replace(tmp, _PENDING_OFFERS)
    except Exception:
        logger.warning("nora_chat_router: failed to consume pending offer")
# //// END Neoffice ////

# Classifier system prompt. Mirrors the SOUL roster domains + its two disambiguation
# rules (any chart/visual → analyse regardless of subject; a plain number in text →
# the owning pole). The model must answer with EXACTLY one lowercase token.
_CLASSIFIER_SYSTEM = (
    "Tu es le routeur de Neoffice. On te donne le message d'un utilisateur. "
    # //// Neoffice — `projet` added to the allowed tokens (20.09). POLES has held it
    # //// since 16.09, but this prompt never named it: the model was asked to pick a
    # //// pole and could not answer this one. Everything the deterministic rules did
    # //// not catch went somewhere else — to a pole that no longer holds a single job
    # //// tool. A pole that cannot be named is a pole that cannot be chosen.
    "Tu réponds par UN SEUL mot parmi : recurrent, compta, projet, ventes, support, rh, analyse, direct. "
    "Aucune ponctuation, aucune explication, juste le mot.\n\n"
    "PRIORITÉ ABSOLUE — 'recurrent' : si la demande doit se RÉPÉTER dans le temps "
    "(« tous les matins / chaque jour / toutes les heures / chaque lundi / chaque semaine / "
    "régulièrement / automatiquement / planifie / programme une tâche / fais-le tous les… »), "
    "réponds 'recurrent' — PEU IMPORTE le sujet (même si ça parle d'emails, de factures ou de "
    "PDF). 'recurrent' EXIGE un marqueur EXPLICITE de répétition ou d'horaire ; une demande "
    "PONCTUELLE (une seule fois, maintenant) n'est JAMAIS 'recurrent'. En particulier "
    "« crée / fais / envoie un rappel » ou « une relance » SANS répétition = un rappel de "
    "paiement ponctuel → compta, PAS 'recurrent'.\n\n"
    "Sinon, choisis le pôle métier qui doit traiter la demande :\n"
    "- compta : factures, paiements, TVA, chiffre d'affaires, impayés, rappels de paiement / "
    "relances / rappels de facture, factures fournisseurs, rapports financiers "
    "(un chiffre demandé en TEXTE).\n"
    # //// Neoffice — invoice allocation and the tenant chart are accounting work.
    "  Cela inclut le plan comptable, le choix d'un compte et l'imputation d'une "
    "facture, d'un ticket, d'un achat ou d'une dépense.\n"
    # //// END Neoffice ////
    # //// Neoffice — Swiss accounting-law questions need the compta worker's
    # curated wiki, even when they do not mention an ERP accounting object.
    "  Cela inclut le droit comptable suisse, la perte de capital, le surendettement "
    "et les art. 725a/725b CO.\n"
    # //// END Neoffice ////
    # //// Neoffice — the job domain, in the classifier's own words (20.09). Placed
    # //// BEFORE ventes because that is where the two compete: both say « devis ».
    # //// The boundary is the JOB, exactly as in the deterministic rules below, where
    # //// a quotation qualified by a chantier is tested before the plain one.
    "- projet : les CHANTIERS et les INTERVENTIONS — ouvrir un chantier, ses visites et "
    "ses rendez-vous, ses lignes de travail, son métré (surfaces, cotes, m²), son "
    "chiffrage, l'atelier, les contrats d'entretien, « ma journée » / « ma tournée », "
    "et les heures ou le matériel posés sur un chantier.\n"
    "  Un devis, une commande ou une facture QUALIFIÉS PAR UN CHANTIER (« le devis de ce "
    "chantier », « facture l'intervention de mardi ») vont à projet et non à ventes : "
    "lui seul tient les outils du chantier. Un devis pour un client SANS chantier reste "
    "à ventes.\n"
    # //// END Neoffice ////
    # //// Neoffice — changing a client's or supplier's contact details is ventes' own tool
    # //// (frappe_party_contact_update, 25.09); support has none. Said here so the classifier
    # //// does not read « e-mail » as support's.
    "- ventes : devis, commandes clients, factures de vente, articles, stock, clients "
    "et fournisseurs (création, recherche, et CHANGEMENT de leur e-mail, téléphone ou "
    "adresse), prix, réapprovisionnement — commandes FOURNISSEURS "
    # //// END Neoffice ////
    "incluses (« commander 15 unités », « passer commande au fournisseur »).\n"
    "- support : emails (lecture/rédaction), pièces jointes & OCR, tickets, "
    "aide à l'utilisation.\n"
    # //// Neoffice — the rh pole's real scope since 2026-09-23 (hr_* tools): what
    # waits for someone's approval, expense claims, where the payroll stands.
    "- rh : congés, notes de frais (déposer, lister, valider), ce qui attend une "
    "validation (congés, notes de frais), paie (état du mois, charges sociales, "
    "fiches de paie), certificats de salaire, fin d'année, employés, contrats, "
    "absences, fins de période d'essai, permis de travail.\n"
    # //// END Neoffice ////
    # //// Neoffice — Swiss HR/payroll doctrine questions need the rh worker's
    # curated wiki (RAG-rh-suisse, wired 2026-08-21): rates and obligations are
    # knowledge questions, not chit-chat, and must not fall to 'direct'.
    "  Cela inclut les taux et obligations RH suisses : AVS/AI/APG, AC, LPP, LAA, "
    "impôt à la source, certificat de salaire, allocations familiales, délais de "
    "congé, vacances et heures supplémentaires, jours fériés, congé paternité, "
    "certificat de travail, attestation de l'employeur (« quel taux AVS ? », « quelle "
    "retenue à la source ? »).\n"
    # //// END Neoffice ////
    "- analyse : graphiques, visuels, dataviz, cartes d'indicateurs, tableaux de bord "
    "sur mesure — QUEL QUE SOIT le sujet (« montre-moi … en graphique »). Une demande "
    "de visualisation va TOUJOURS à analyse, jamais à compta/ventes.\n\n"
    "Réponds 'direct' UNIQUEMENT si le message est une salutation, un remerciement, du "
    "bavardage, une question sur Nora elle-même, ou une demande à laquelle on répond en "
    "une phrase SANS consulter les données métier. En cas de doute entre un pôle et "
    "'direct' pour une vraie demande métier, choisis le pôle.\n\n"
    "CAPACITÉ vs DEMANDE — distingue deux formulations proches :\n"
    "- Le message demande ce que Nora SAIT FAIRE, sans donnée ni information à chercher "
    "(« est-ce que tu peux créer un client ? », « tu sais gérer les devis ? », "
    "« c'est possible d'envoyer une relance ? ») → 'direct' : la réponse est une phrase, "
    "mobiliser un pôle coûte 20 secondes pour finir par redemander les informations.\n"
    "- Le message demande une INFORMATION à aller chercher, ou FOURNIT les données, ou "
    "donne un ORDRE (« peux-tu me dire le montant de la dernière facture ? », "
    "« crée le client X, email contact@exemple.ch », « envoie la relance à Martin ») "
    "→ le pôle concerné, même si la phrase commence par « est-ce que tu peux ».\n\n"
    "SUITE DE CONVERSATION : si un contexte « message précédent → pôle X » t'est donné ET "
    "que le nouveau message est une PRÉCISION/SUITE (un nom seul, « ceux de … », « et pour … », "
    "un filtre, un pronom comme « les siens »), garde le MÊME pôle X. Un nom de personne dans un "
    "contexte ventes/devis ou compta/factures est un CLIENT, PAS un sujet RH (rh = congés, paie, "
    "employés internes uniquement)."
)

# A classifier reply token → canonical pole (or "DIRECT"). We accept the bare key.
_TOKEN_RE = re.compile(r"[a-zàâçéèêëîïôûùüÿñæœ]+", re.IGNORECASE)

# ── Keyword fast-path (latency) ──────────────────────────────────────────────
# Route the OBVIOUS messages WITHOUT the ~1s LLM classifier call → the ack lands
# instantly. CONSERVATIVE on purpose: only UNAMBIGUOUS triggers are here. Ambiguous
# words ("facture", "client", "prix", "paiement") are deliberately absent — they fall
# through to the LLM (with the full SOUL) so routing quality is never sacrificed for
# speed. The fast-path is SKIPPED entirely when:
#   - there is a follow-up `prior` pole → the LLM-with-context path must handle it
#     ("Ceux de ce client" must stay on the prior pole), and
#   - the message carries recurrence markers → the 'recurrent' decision belongs to the LLM.
_RECUR_RE = re.compile(
    r"(tous les|chaque (jour|matin|soir|semaine|lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche|mois)|"
    r"toutes les heures|r[ée]guli[èe]rement|automatiquement|planifie|programme[ -]?(moi|une|une t)|"
    r"fais[ -]?le tous|chaque fois|"
    # //// Neoffice — the habit markers of the three other languages of the fleet. French
    # //// only, « Remind me every Monday at 10:00 » was a ONE-OFF reminder next Monday.
    r"\b(?:every|each)\s+(?:day|morning|evening|night|week|month|hour|weekday|monday|tuesday|wednesday|"
    r"thursday|friday|saturday|sunday)s?\b|\bon\s+(?:mon|tues|wednes|thurs|fri|satur|sun)days\b|"
    r"\b(?:daily|weekly|monthly|hourly)\b(?=\s*(?:$|[,.;:!?]|at\b|on\b|to\b|from\b))|"
    r"\bjede[nrs]?\s+(?:tag|morgen|abend|woche|monat|stunde|montag|dienstag|mittwoch|donnerstag|freitag|"
    r"samstag|sonntag)\b|\b(?:t[äa]glich|w[öo]chentlich|monatlich|st[üu]ndlich|werktags|montags|dienstags|"
    r"mittwochs|donnerstags|freitags|samstags|sonntags)\b|"
    r"\bogni\s+(?:giorno|mattina|sera|settimana|mese|ora|luned[iì]|marted[iì]|mercoled[iì]|gioved[iì]|"
    r"venerd[iì]|sabato|domenica)\b|\btutti\s+i\s+giorni\b|"
    r"\b(?:quotidiennement|hebdomadairement|mensuellement|en semaine|du lundi au vendredi)\b)",
    # //// END Neoffice ////
    re.IGNORECASE,
)
# (regex, pole) — first match wins. ANALYSE is checked FIRST: a chart/visual request goes
# to analyse regardless of subject (SOUL rule). Then the unambiguous domain keywords.
# //// Neoffice — booking a visit is a JOB matter, whatever words it contains (16.09).
# « Je passe chez <client> demain à 14h30 pour changer une carte graphique » routed to
# ANALYSE: the chart keyword matched « carte graphique », a piece of hardware. The
# analyse worker holds no job tool, so it blocked for a partner and the user got
# nothing — for the most ordinary request a tradesman makes. Narrowing the chart rule
# would only postpone it (« diagramme de câblage », « tableau de bord » of a car): a
# sentence that books a visit belongs to ventes, and it is decided BEFORE the keyword
# rules. Deliberately narrow: someone must be GOING somewhere, or a rendez-vous must
# be named. « Fais-moi un graphique des heures » is untouched.
_APPOINTMENT_RE = re.compile(
    r"\b(?:je|on|il|elle|tu)\s+(?:dois|doit|vais|va|passe|passons|serai|sera|repasse)\s+"
    r"(?:\w+\s+){0,2}?(?:chez|voir|sur place|au domicile)\b"
    r"|\bpasser\s+(?:\w+\s+){0,2}?chez\b"
    r"|\brendez[-\s]?vous\b"
    r"|\b(?:intervention|d[ée]placement|visite)\s+(?:chez|pour|le|demain|lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche)\b",
    re.IGNORECASE,
)
# //// Neoffice — reporting work already done ("j'ai été chez X", "j'ai passé deux
# heures", "j'ai pris un joint") is the same job matter as booking one, and it hits
# the same trap from the other side: « heures » is an HR word, « pris » a stock one.
# Measured 16.09: it landed on RH, which has no job tool, and the worker repeated
# one call until the guardrail answered the customer in English. Narrow on purpose:
# the speaker must be reporting what THEY did, in the past.
_J_AI = r"j\s*['’]?\s*ai"
_DID_WORK_RE = re.compile(
    # _J_AI covers j'ai, j’ai and j ai — see above.
    # //// Neoffice — and the accents go the same way as the apostrophe: this is
    # //// typed one-handed on a phone, on site. « j ai ete sur place » is the same
    # //// sentence as « j'ai été sur place » and must not route differently.
    r"\b" + _J_AI + r"\s+(?:\w+\s+){0,2}?(?:pass[ée]|rest[ée]|boss[ée]|travaill[ée])\b"
    # //// Neoffice — « sur place » / « sur le chantier » say the same thing as « chez »
    # //// and are what the field actually types: « j'ai été sur place, deux heures ».
    # //// Without them that sentence matched no rule at all, fell to the classifier,
    # //// and « heures » made it an HR matter — the very mistake _JOB_ORDER_RE below
    # //// was written to undo. The repo test for it has been red since it was written.
    r"|\b" + _J_AI + r"\s+(?:[ée]t[ée]|fait un tour|fini)\s+(?:\w+\s+){0,2}?(?:chez|sur\s+place|sur\s+le\s+chantier)\b"
    r"|\bje\s+(?:sors|reviens|rentre)\s+de\s+chez\b"
    r"|\b" + _J_AI + r"\s+pris\s+(?:un|une|des|le|la|deux|trois)\b",
    re.IGNORECASE,
)
# //// Neoffice — the same job matter given as an ORDER, not as a story. The two
# rules above only know the first person ("j'ai passé", "je passe chez"), so
# « Note une heure de travail sur le chantier PROJ-0087 » fell through to the
# keyword rules, where « heures » is an HR word: it landed on RH, which holds no
# job tool, and the worker looped until the guardrail answered the customer in
# English (measured 16.09). ANCHORED on the job — an imperative alone is not
# enough, or « ajoute deux heures de congé » would be stolen from RH, which is
# exactly the mistake this rule exists to undo.
_JOB_ORDER_RE = re.compile(
    # //// Neoffice — opening a job and asking about one are orders too (17.09):
    # //// « crée un chantier pour une rénovation » and « demande l'avis d'un expert
    # //// sur ce chantier » matched no rule and depended on the classifier answering
    # //// within its eight seconds. Still ANCHORED on the job, so « crée un client »
    # //// is untouched.
    r"\b(?:note|notez|enregistre|enregistrez|pointe|pointez|saisis|saisissez|"
    r"inscris|inscrivez|ajoute|ajoutez|rajoute|rajoutez|met[s]?|mettez|"
    r"cr[ée]e|cr[ée]ez|cr[ée]er|ouvre|ouvrez|ouvrir|d[ée]marre|d[ée]marrez|"
    r"lance|lancez|demande|demandez)\b"
    r"[^.!?]{0,80}?\b(?:chantier|intervention|PROJ-\d+)\b"
    r"|\bPROJ-\d+\b[^.!?]{0,80}?\b(?:heure|heures|\dh\b|main[-\s]d.?oeuvre|"
    r"mat[ée]riel|fourniture)",
    re.IGNORECASE,
)
# //// END Neoffice ////


# //// Neoffice — the job pole's own QUESTIONS. See the rule that uses this (below,
# //// in _FAST_PATH_RULES) for why each half is shaped the way it is.
_JOB_QUESTIONS_RE = re.compile(
    r"\b(?:quels?|quelles?|combien\s+de|liste|listez|montre|montrez|affiche|affichez)\b"
    r"[^.!?]{0,24}?\bchantiers\b"
    r"|\b(?:natures?|types?|sortes?)\s+de\s+chantiers?\b"
    r"|\bma\s+(?:journ[ée]e|tourn[ée]e)\b"
    r"|\bj\s*['’]?\s*ai\s+le\s+temps\b"
    r"|\blignes?\s+de\s+travail\b",
    re.IGNORECASE,
)
# //// END Neoffice ////


# //// Neoffice — what a quantity-surveying question looks like. Kept beside the rules
# //// it feeds so the two halves stay readable; see the rule for why they differ.
_DIMENSION = (
    r"\d+(?:[.,]\d+)?\s*(?:mm|cm|m|m[èe]tres?)?\b[^.!?]{0,25}?"
    r"(?:\bx\b|\*|\bsur\b|\bpar\b)\s*\d"
)
_MEASURE_NOUN = r"(?:surface|superficie|quantit[ée]|volume|p[ée]rim[èe]tre)"
_MEASUREMENT_RE = re.compile(
    r"\b(?:m[ée]tr[ée]s?|m[ée]trer|m[ée]trage|cubage)\b"
    r"|\bm[²2³3]\b"
    r"|\bm[èe]tres?\s+(?:carr[ée]s?|cubes?|lin[ée]aires?)\b"
    r"|\b" + _MEASURE_NOUN + r"\w*\b[\s\S]{0,200}?" + _DIMENSION +
    r"|" + _DIMENSION + r"[\s\S]{0,200}?\b" + _MEASURE_NOUN + r"\w*\b",
    re.IGNORECASE,
)
# //// END Neoffice ////

_FAST_PATH_RULES = (
    (re.compile(r"(graphique|en graphique|visuel|visualise|dataviz|tableau de bord|histogramme|camembert|courbe|diagramme)", re.IGNORECASE), "analyse"),
    # //// Neoffice — CHANGING a customer's or supplier's e-mail, phone or address is ventes'
    # //// (frappe_party_contact_update). « Change l'adresse e-mail de <un client> »
    # //// reached support, which has no such tool: tool_search five times (bench, 24.09).
    # //// Not for an employee (rh keeps personnel records).
    (
        re.compile(
            r"^(?!.*\b(?:employ[ée]e?|collaborat\w*|salari[ée]e?|mitarbeiter\w*|dipendente)\b)"
            r".*\b(?:change[rz]?|modifie[rz]?|met[sz]?\s+[àa]\s+jour|corrige[rz]?|remplace[rz]?|update|"
            r"[äa]ndere|aggiorna)\b.{0,40}?\b(?:e-?mail|adresse\s+e-?mail|courriel|t[ée]l[ée]phone|"
            r"num[ée]ro\s+de\s+(?:t[ée]l[ée]phone|portable|mobile)|mobile|natel|adresse|email|phone|address|"
            r"telefon|indirizzo)\b",
            re.IGNORECASE,
        ),
        "ventes",
    ),
    # //// END Neoffice ////
    # //// Neoffice — the same change told as a FACT, with no « change » verb (25.09):
    # //// « Martin SA a une nouvelle adresse e-mail : … », « … a changé d'adresse e-mail »,
    # //// « nouveau numéro de téléphone pour … », « … a déménagé : rue du Lac 12 ». The first
    # //// two reached support through the e-mail rule below; the others fell to the
    # //// classifier. Never when the message asks to SEND something (« écris un courriel à
    # //// la nouvelle adresse de Martin SA » stays support), never for an employee (rh keeps
    # //// personnel records), and « a déménagé » only in the third person: « nous avons
    # //// déménagé » is the company itself, not a client.
    (
        re.compile(
            r"^(?!.*\b(?:employ[ée]e?|collaborat\w*|salari[ée]e?|mitarbeiter\w*|dipendente)\b)"
            r"(?!.*(?:\benvoi|\benvoy|\b[ée]cri[rstvez]|\br[ée]dig|\btransmet|\badresse-(?:lui|leur|moi)\b))"
            r".*(?:\bnouve(?:lle|au|l)s?\s+(?:adresse(?:\s+e-?mail)?|e-?mail|courriel|"
            r"num[ée]ro(?:\s+de\s+(?:t[ée]l[ée]phone|portable|mobile))?|t[ée]l[ée]phone|mobile|natel)\b"
            r"|\ba\s+chang[ée]\s+d['’]\s*(?:adresse|e-?mail|num[ée]ro|t[ée]l[ée]phone)"
            r"|\ba\s+d[ée]m[ée]nag[ée]\b"
            r"|\bchangement\s+d['’]\s*(?:adresse|e-?mail|num[ée]ro))",
            re.IGNORECASE,
        ),
        "ventes",
    ),
    # //// END Neoffice ////
    # //// Neoffice — MY pay is rh's, never a revenue question (capability bench, 24.09):
    # //// « Combien ai-je touché en août ? » from an employee reached compta, which called
    # //// the company's revenue summary five times. First person with touché/gagné/perçu
    # //// only — « combien ai-je encaissé / reçu » is the company's money — or a possessive
    # //// pay noun.
    (
        re.compile(
            r"\bcombien\s+(?:ai[- ]je|j['’]ai)\s+(?:touch[ée]|gagn[ée]|per[çc]u)\b"
            r"|\b(?:mon|mes)\s+(?:salaires?|net|brut)\b|\b(?:ma|mes)\s+(?:paies?|fiches?\s+de\s+(?:paie|salaire))\b"
            r"|\bmy\s+(?:salary|pay|payslips?|wages?)\b|\bhow\s+much\s+(?:did|have)\s+i\s+(?:earn|been\s+paid|got\s+paid)"
            r"|\bmein(?:e|en)?\s+(?:lohn|gehalt|lohnabrechnung(?:en)?)\b|\b(?:il\s+mio\s+stipendio|la\s+mia\s+busta\s+paga)\b",
            re.IGNORECASE,
        ),
        "rh",
    ),
    # //// END Neoffice ////
    # //// Neoffice — chasing a JOB's quotation is projet's, not a collection (17.09).
    # //// Same cause as the prospect rule below: the dunning rule matches \brelanc\w*
    # //// and sits above the quotation rules, so « relance le devis du chantier PROJ-… »
    # //// reached compta — which holds the dunning tools and not one job gesture, while
    # //// projet holds frappe_job_quote and frappe_job_customer_said_yes. Pre-existing,
    # //// measured 17.09 while testing the prospect rule.
    # //// THREE lookaheads, like the mail rule above: the message must carry the chasing
    # //// verb AND a quotation noun AND a job. « relance de paiement pour le chantier »
    # //// has no quotation noun and stays compta, which is the whole point.
    (
        re.compile(
            r"(?=.*\brelanc\w*\b)"
            r"(?=.*\b(?:devis|offres?)\b)"
            r"(?=.*\b(?:chantier|intervention|PROJ-\d+)\b)",
            re.IGNORECASE,
        ),
        "projet",
    ),
    # //// END Neoffice ////
    # //// Neoffice — chasing a QUOTATION with no job is ventes' (24.09): ventes holds
    # //// send_email and frappe_quotation_share_link, the acceptance link a follow-up
    # //// carries. The dunning rule below sent « relance le devis de Martin » to compta,
    # //// whose reminders are for INVOICES (capability bench, 24.09). The job rule above
    # //// still wins for « relance le devis du chantier ».
    (
        re.compile(
            r"(?=.*\brelanc\w*\b)(?=.*\b(?:devis|offres?|quotations?|DEVIS-\d+)\b)",
            re.IGNORECASE,
        ),
        "ventes",
    ),
    # //// END Neoffice ////
    # //// Neoffice — relancer un PROSPECT is a sales follow-up, not a collection (17.09).
    # //// The dunning rule just below matches \brelanc\w* — deliberately broad, because a
    # //// payment reminder is phrased a dozen ways — and it was swallowing « relance ce
    # //// prospect », which belongs to ventes. Measured that day: it reached compta, which
    # //// holds the dunning tools and not one sales gesture.
    # //// Deliberately NARROW: only « prospect » and « lead », the two words that can only
    # //// mean a sale. « devis » is left out on purpose — « relance le devis du chantier »
    # //// must keep reaching projet through the quotation rule further down, and a bare
    # //// « relance-le » or « relance la facture » stays compta, as it should.
    (
        re.compile(
            r"\brelanc\w*\b[^.!?]{0,30}?\b(?:prospects?|leads?)\b"
            r"|\b(?:prospects?|leads?)\b[^.!?]{0,30}?\brelanc\w*\b",
            re.IGNORECASE,
        ),
        "ventes",
    ),
    # //// END Neoffice ////
    # //// Neoffice — payment-reminder ("rappel de paiement" / "relance" / "rappel de facture")
    # routes DETERMINISTICALLY to compta. A dunning/Payment Reminder is a collections matter
    # (accounting), NOT a support ticket; the frappe_payment_reminder_create tool lives in compta
    # AND ventes (neoffice-devops commit c933017), never support. The phrasing is lexically
    # ambiguous to the LLM classifier (rappel→support, paiement/impayé→compta) so the model must
    # NOT decide it — a misroute to support lands on a pole without the tool → junk draft (observed
    # live). Placed ABOVE the generic compta rule (which matches "impayé" but not "relance"); a
    # plain "combien d'impayés ?" still hits compta below. grep "//// Neoffice".
    (re.compile(r"rappel[s]?\s+de\s+(paiement|facture)|lettre[s]?\s+de\s+relance|\brelanc\w*|\bDUNN-\w+", re.IGNORECASE), "compta"),
    # //// END Neoffice ////
    # //// Neoffice — SENDING/WRITING an email routes DETERMINISTICALLY to support, the ONLY
    # pole with the email tools (send_email/confirm_send_email/modify_email_draft live in
    # DOMAIN_WRITES["support"]). The intent "envoie/écris/rédige/transmets … un mail/e-mail/
    # courriel" is lexically ambiguous to the LLM classifier when the email is ABOUT a business
    # object ("envoie un email pour la FACTURE FA-…" pulls it to compta, "… pour le DEVIS …" to
    # ventes) → it landed on a pole with NO email tool → the worker answered "je ne peux pas
    # envoyer" (the 2026-06-30 bug). Requires BOTH a compose/send verb AND an email noun (two
    # lookaheads) so a plain "envoie-moi le chiffre d'affaires" is NOT caught (it falls through
    # to the compta rule below). Placed ABOVE the compta/ventes rules so the email-ABOUT-a-doc
    # case wins; placed BELOW the dunning rule so "envoie un rappel de paiement" stays compta
    # (a dunning, not a free email). Reading mail is also a support matter, but we only fast-path
    # the SEND/WRITE intent here — pure reads fall through to the LLM (support per its prompt).
    # grep "//// Neoffice".
    (re.compile(
        r"(?=.*\b(?:(?:e-?)?mails?|courriels?|courrier\s+[ée]lectronique)\b)"
        # //// Neoffice — « adresse » the NOUN is not a send verb (25.09). A bare `adress`
        # //// here matched « l'adresse e-mail », so « Martin SA a une nouvelle adresse
        # //// e-mail » and « … a changé d'adresse e-mail » reached support, which holds no
        # //// tool to change a contact. Only the verb's own forms count now: adresser,
        # //// adressez…, « adresse-lui », « adresse un mail ».
        r"(?=.*(?:envoi|envoy|[ée]cri[rstvez]|r[ée]dig|transmet|transmettre"
        r"|\badress(?:er|ez|ons|ent|ée?s?)\b|\badresse-(?:lui|leur|moi|nous|les)\b"
        r"|\badresse\s+(?:un|une|ce|cet|cette|le|la|les|mon|ma|mes|son|sa|ses|notre|votre|leur)\s+"
        r"(?:e-?mails?|mails?|courriels?|messages?|lettres?)\b))",
        # //// END Neoffice ////
        re.IGNORECASE,
    ), "support"),
    # //// END Neoffice ////
    # //// Neoffice — invoice allocation must skip the fallible LLM classifier.
    # The first production E2E attempt spent 107s retrying the router model and
    # never reached compta, even though "plan comptable" is unambiguous. Require
    # either that exact domain phrase, or both an accounting object and an
    # allocation/account cue, so "à qui imputer cette erreur ?" stays untouched.
    (
        re.compile(
            r"\bplan\s+comptable\b|\bimputation\s+comptable\b|"
            r"(?=.*\b(?:factur\w*|tickets?\b|d[ée]pens\w*|achats?\b))"
            r"(?=.*\b(?:imput\w*|comptes?\s+(?:de\s+)?(?:charge|comptable)))",
            re.IGNORECASE,
        ),
        "compta",
    ),
    # //// END Neoffice ////
    # //// Neoffice — Swiss accounting law must reach the compta worker, which
    # owns the curated doctrine wiki. Keep this separate from the generic rule
    # below so future upstream rebases leave the original matcher untouched.
    (
        re.compile(
            r"(perte de capital|surendettement|art(?:icle)?\.?\s*725[ab]?\b|\b725[ab]\s+CO\b)",
            re.IGNORECASE,
        ),
        "compta",
    ),
    # //// END Neoffice ////
    # //// Neoffice — a job named or ASKED ABOUT belongs to `projet`. The three
    # //// rules above the table only know the imperative ("note une heure sur le
    # //// chantier…") and the first person ("j'ai passé…"); a plain QUESTION —
    # //// « quel est le statut du chantier PROJ-0091 ? » — matched no rule at all
    # //// and fell through to the LLM classifier, which sent it to ventes, the
    # //// pole that owned job matters until 16.09. Ventes answered honestly that
    # //// it holds no job tool (measured 16.09).
    # //// Deliberately narrow, in two halves that fail differently:
    # ////   · a job NUMBER is unambiguous — nothing else in the system is PROJ-n;
    # ////   · a job NOUN only counts next to a state word. « chantier » alone
    # ////     would steal « recrute un ouvrier pour le chantier » from rh.
    # //// Below the email and dunning rules on purpose: « envoie un mail au sujet
    # //// du chantier X » is still support's, which alone holds the mail tools.
    (
        re.compile(
            r"\bPROJ-\d+\b"
            r"|\b(?:statut|[ée]tat|avancement|o[uù]\s+(?:en\s+est|ça\s+en\s+est|ca\s+en\s+est)|"
            r"point\s+sur)\b[^.!?]{0,40}?\b(?:chantier|intervention)\b"
            r"|\b(?:chantier|intervention)\b[^.!?]{0,40}?\b(?:statut|[ée]tat|avancement|"
            r"o[uù]\s+en\s+est)\b",
            re.IGNORECASE,
        ),
        "projet",
    ),
    # //// END Neoffice ////
    # //// Neoffice — a MEASUREMENT belongs to `projet`, which alone holds frappe_measure
    # //// and frappe_measure_onto_line. Without this rule a plain surveying question
    # //// — « trois murs de 4.20 m sur 2.50 m, moins une porte, quelle surface ? » —
    # //// matched nothing, fell through to the LLM classifier, and came back DIRECT:
    # //// answered by the gateway agent, which holds not one of the 48 pole tools, so
    # //// the arithmetic was improvised in prose instead of computed (measured 17.09).
    # //// Two halves that fail differently, on purpose:
    # ////   · a trade word nothing else in the system says (métré, cubage, m²) — enough
    # ////     on its own, because no other pole has a use for it;
    # ////   · a measurement NOUN only counts next to a dimension arithmetic. « surface
    # ////     de vente du magasin » carries no figures and stays where it was.
    # //// Above the `devis` and `chiffre d affaires` rules so « le métré pour le devis »
    # //// is measured before it is priced; below the mail rules, like the job rules
    # //// above — « envoie le métré par mail » is still support, which holds the mail tools.
    (_MEASUREMENT_RE, "projet"),
    # //// END Neoffice ////
    # //// Neoffice — the job pole's own questions, made deterministic (17.09). Measured
    # //// that day: the gateway gives the classifier EIGHT seconds and falls back to
    # //// DIRECT when it does not answer — 33 times in this log, 14 on that day alone.
    # //// DIRECT means the gateway agent replies, and it holds not one pole tool, so a
    # //// sentence that matters must not depend on the model answering in time.
    # //// Narrow, each half failing differently:
    # ////   · a QUESTION about jobs in the PLURAL — « quels chantiers … ». The plural is
    # ////     the discriminator: « recrute un ouvrier pour le chantier » is singular AND
    # ////     carries no question word, so it stays rh, which is the collision the job
    # ////     rules above exist to avoid;
    # ////   · « natures / types de chantier » — vocabulary nothing else in the system uses;
    # ////   · « ma journée » / « ma tournée » — the field worker's own day
    # ////     (frappe_my_day, frappe_close_my_day), never anyone else's;
    # ////   · « j'ai le temps » — frappe_can_i_make_it, a scheduling question;
    # ////   · « ligne de travail » — the pole's own noun for what it writes.
    (_JOB_QUESTIONS_RE, "projet"),
    # //// END Neoffice ////
    (re.compile(r"(chiffre d'affaires|chiffre d affaires|\btva\b|impay[ée]|\bbilan\b|grand livre|écritures? comptables?|factures? fournisseur)", re.IGNORECASE), "compta"),
    # //// Neoffice — a payment RECORDED against an invoice is compta's, and nothing
    # //// claimed it: « enregistre le paiement de la facture » matched no rule and
    # //// fell to the classifier. Deliberately the PAIR (payment + invoice), never
    # //// « facture » alone — that word alone would steal « fais une facture pour ce
    # //// client » from ventes. Below the dunning rule, which owns « rappel de
    # //// paiement » and is tested first. The repo test for it has been red.
    (
        re.compile(
            r"\b(?:paiement|encaissement|r[èe]glement)\b[^.!?]{0,40}?\bfactures?\b"
            r"|\bfactures?\b[^.!?]{0,40}?\b(?:paiement|encaissement|r[èe]glement)\b",
            re.IGNORECASE,
        ),
        "compta",
    ),
    # //// END Neoffice ////
    # //// Neoffice — a quotation QUALIFIED BY A JOB belongs to `projet`, and must be
    # //// tested before the plain `devis` rule below, which would otherwise swallow it.
    # //// « compose le devis de ce chantier » is the sentence that failed on 15.09: it
    # //// reached ventes, whose generic quotation tool produced a ONE-LINE document
    # //// (a single generic service line named after the project) while the
    # //// job's own costing was empty. `projet` holds frappe_job_quote and the
    # //// composition that reads the ouvrages. An unqualified « fais un devis pour
    # //// un client » stays a sales quotation and still falls through to ventes.
    (
        re.compile(
            r"\b(devis|offre)\b.{0,40}\b(chantier|projet|intervention|PROJ-\d+)\b"
            r"|\b(chantier|projet|intervention|PROJ-\d+)\b.{0,40}\b(devis|offre)\b",
            re.IGNORECASE,
        ),
        "projet",
    ),
    # //// END Neoffice ////
    # //// Neoffice — a line ADDED, CHANGED or REMOVED on an invoice, a quotation or an
    # //// order is ventes' (#681, 2026-09-24). « Ok, est-ce que tu peux rajouter un EAP
    # //// huit cent trente à cette facture ? » matched no rule, so the GO-AHEAD guard read
    # //// its « Ok » as a confirmation and kept it on the previous pole, compta, whose
    # //// worker swapped the article. Jérémy's call: editing a sales document's lines is
    # //// a sales gesture. A rule here also releases that guard, which yields to any
    # //// other pole's rule. Needs the verb AND a preposition before the document, so
    # //// « mets la facture en brouillon » stays with the classifier; a payment on an
    # //// invoice (compta) and a job's quotation (projet) are matched above, first.
    (
        re.compile(
            r"\b(?:r?ajout\w*|mets|mettez|mettre|enl[èe]v\w*|retir\w*|supprim\w*|modifi\w*|"
            r"chang\w*|remplac\w*|corrig\w*)\b"
            r"[^.!?]{0,80}?\b(?:[àa]|au|aux|sur|dans|de|du|des)\s+"
            r"(?:la\s+|le\s+|les\s+|cette\s+|ce\s+|cet\s+|l['’]\s*|ma\s+|mon\s+|notre\s+)?"
            r"(?:factur\w*|devis|commandes?|offres?)\b",
            re.IGNORECASE,
        ),
        "ventes",
    ),
    # //// END Neoffice ////
    (re.compile(r"(\bdevis\b|commande[s]? client|bon de commande client)", re.IGNORECASE), "ventes"),
    # //// Neoffice — expense claims, salary certificates and source tax go to rh
    # (2026-09-23): the pole now holds the tools to file, list and decide an expense
    # claim and the payroll recaps. « note de frais » used to fall to the classifier,
    # which sent it to compta; compta keeps the same expense tools as a fallback.
    (
        re.compile(
            r"(cong[ée]s?\b|fiche de paie|bulletin de salaire|\bpaie\b|absences? (du|des)"
            r"|notes? de frais|certificats? de salaire|imp[ôo]ts? [àa] la source"
            # //// Neoffice — Swiss HR doctrine the rh worker holds in its wiki (24.09): a
            # //// public-holiday question was answered by the orchestrator itself, citing the
            # //// wrong article. « 1er août » only next to a holiday word, so « facture la
            # //// livraison du 1er août » stays where it was.
            r"|jours? f[ée]ri[ée]s?|f[êe]te nationale|\b1(?:er)?\s+ao[ûu]t\s+(?:est|f[ée]ri|pay|ch[ôo]m)"
            r"|certificats? de travail|attestations? de travail"
            r"|attestations? (?:de l['’]\s*employeur|(?:pour (?:le|la caisse de) )?ch[ôo]mage))",
            re.IGNORECASE,
        ),
        "rh",
    ),
    # //// END Neoffice ////
)
# DIRECT only when the WHOLE message is a greeting/thanks/meta (so "Bonjour, quel est mon
# CA ?" is NOT caught here — the domain rules above match "chiffre d'affaires" first).
_DIRECT_RE = re.compile(
    r"^\s*(bonjour|salut|coucou|hello|hey|merci[\s!.]*|ça va|ca va|comment vas[ -]?tu|qui es[ -]?tu|que sais[ -]?tu faire)[\s!.?]*$",
    re.IGNORECASE,
)


# //// Neoffice — the pole an EXPLICIT rule claims, or None. No LLM, no context:
# //// this is the deterministic half of the module, in ONE place so the first
# //// turn and a follow-up can never disagree about what the rules say.
# ////
# //// The three job rules come FIRST: a visit and a report of work done win over
# //// any keyword they happen to contain. All three go to `projet`, not `ventes`
# //// — the fourteen building-job writes (frappe_job_book_visit, frappe_job_quick,
# //// frappe_job_record_work and the rest) moved to `projet` on 16.09. Routed to
# //// ventes they would reach a worker that no longer holds a single one of them,
# //// and the user would get the guard's English text instead of an answer.
# //// Change them together with DOMAIN_WRITES, never one without the other.
def _keyword_pole(msg: str) -> Optional[str]:
    if _APPOINTMENT_RE.search(msg):
        logger.info("nora_chat_router: booking a visit → projet (keyword rules skipped)")
        return "projet"
    if _DID_WORK_RE.search(msg):
        logger.info("nora_chat_router: work already done → projet (keyword rules skipped)")
        return "projet"
    if _JOB_ORDER_RE.search(msg):
        logger.info("nora_chat_router: job order → projet (keyword rules skipped)")
        return "projet"
    for rx, pole in _FAST_PATH_RULES:
        if rx.search(msg):
            return pole
    return None
# //// END Neoffice ////

# //// Neoffice — a question about what NORA CAN DO, carrying no concrete data.
# Used ONLY to DEFER to the LLM classifier (never to decide): the domain keyword in
# "tu sais gérer les devis ?" is the subject, not a task. Guards keep real work out:
# a figure or an @ means data was supplied ("crée le client Jean, jean@x.ch"), and the
# message must be a short question. grep "//// Neoffice".
# //// Neoffice — a ONE-OFF reminder for the person asking goes to NORA herself (24.09).
# //// « Rappelle-moi demain à 10 h d'appeler Dupont » had no tool and no route: the
# //// classifier reads « rappel » as a payment reminder (compta), and the keyword rule
# //// `relanc` sends « rappelle-moi lundi de relancer X » to the dunning pole. NORA now
# //// holds nora_reminder_create (Frappe's own Reminder). Two conditions, both needed: a
# //// reminder verb AND a moment (« demain », « à 10 h », « lundi », « dans 2 heures »…),
# //// so « rappelle-moi combien on a facturé » (tell me again) is not taken for one; a
# //// payment reminder keeps compta, and a repeated one stays with the classifier.
_ONE_OFF_REMINDER_RE = re.compile(
    # //// Neoffice — « note-moi de … vendredi » is a reminder too (capability bench, 24.09: it
    # //// reached compta, which has no reminder tool, and looped on tool_search).
    r"\b(?:rappelle[rz]?[- ]moi|fais[- ]moi\s+penser|note[sz]?[- ]moi\s+(?:de|d['’]|que)|"
    r"(?:mets|mettre|mettez|cr[ée]e[rz]?|programme[rz]?|"
    r"ajoute[rz]?)[- ](?:moi\s+|nous\s+)?un\s+rappel|erinnere?\s+mich|ricordami|remind\s+me)\b",
    re.IGNORECASE,
)
_REMINDER_MOMENT_RE = re.compile(
    r"\b(?:demain|apr[èe]s-demain|ce\s+soir|cet\s+apr[èe]s-midi|ce\s+matin|tout\s+[àa]\s+l'heure|"
    r"lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche|la\s+semaine\s+prochaine|"
    r"dans\s+\d+\s*(?:min|minutes?|h|heures?|jours?|semaines?)|[àa]\s+\d{1,2}\s*(?:h|:)|"
    r"\d{1,2}\s*h\s*\d{0,2}\b|\d{1,2}:\d{2}|le\s+\d{1,2}(?:er)?[\s./]|morgen|domani|tomorrow|tonight|"
    # //// Neoffice — the moments of the three other languages: nora's route_reminder
    # //// reads them all (task_router._reminder_moment).
    r"(?:[üu]ber|ueber)morgen|dopodomani|day\s+after\s+tomorrow|heute|oggi|today|stasera|stamattina|"
    r"in\s+\d+\s*(?:min\w*|hours?|days?|weeks?|stunden?|tagen?|wochen?)|(?:tra|fra)\s+\d+\s*(?:minuti|ore|"
    r"giorni|settimane)|\d{1,2}(?:[.:]\d{2})?\s*uhr|(?:um|alle|at)\s+\d{1,2}\b|\d{1,2}(?::\d{2})?\s*[ap]\.?m\b|"
    r"montag|dienstag|mittwoch|donnerstag|freitag|samstag|sonntag|luned[iì]|marted[iì]|mercoled[iì]|"
    r"gioved[iì]|venerd[iì]|sabato|domenica|monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"n[äa]chste[n]?\s+woche|next\s+week|prossima\s+settimana|settimana\s+prossima)",
    # //// END Neoffice ////
    re.IGNORECASE,
)
_PAYMENT_REMINDER_RE = re.compile(r"rappel[s]?\s+de\s+(?:paiement|facture)", re.IGNORECASE)


def _is_one_off_reminder(msg: str) -> bool:
    """A reminder verb AND a moment, not a payment reminder, not a repeated one."""
    msg = msg or ""
    return bool(
        _ONE_OFF_REMINDER_RE.search(msg)
        and _REMINDER_MOMENT_RE.search(msg)
        and not _PAYMENT_REMINDER_RE.search(msg)
        and not _RECUR_RE.search(msg)
    )


# Handed to the orchestrator with the message. Routing it to NORA was not enough: on the dev
# instance NORA held the tool and still delegated the reminder to the support pole with
# kanban_create, then answered « C'est noté » with nothing written (2026-09-24). The code
# decided it is a reminder; the instruction says so at the decision point.
# //// Neoffice — a recurring request at a cadence the scheduler cannot run (« tous les 15
# //// jours »). Recovered to a pole as a one-shot, it ran the job once and never mentioned
# //// the cadence (dev instance, 2026-09-24). The orchestrator explains and asks; it sees
# //// the conversation, so « chaque lundi alors » that follows keeps the original request.
_UNSUPPORTED_CADENCE_HINT = (
    "[Route: the person asked for a RECURRING task at a cadence the scheduler cannot run "
    "(every N days or weeks, several times a day, a day of the month other than the 1st, the "
    "end of the month). Do NOT run the task now and do NOT call kanban_create. In their "
    "language, say this cadence is not available, list what is — every hour, every day, "
    "weekdays, given days of the week (e.g. every Monday and Thursday), the 1st of every "
    "month — and ask which one they want. When they choose, call nora_schedule_task with that "
    "cadence and their original instruction.]"
)
# //// END Neoffice ////
_ONE_OFF_REMINDER_HINT = (
    "[Route: a ONE-OFF reminder for the person asking. Call nora_reminder_create yourself, now "
    "(when=\"YYYY-MM-DD HH:MM\" from the date and time given below, what=their words, user and "
    "conversation_id as given); do NOT call kanban_create. Then say its at_spelled back. If the "
    "call fails, say so: never answer that it is noted when nothing was written.]"
)
# //// END Neoffice ////


_CAPABILITY_RE = re.compile(
    r"^\s*(?:est[-\s]ce\s+que\s+)?"
    r"(?:tu\s+(?:peux|sais|pourrais)|peux[-\s]tu|sais[-\s]tu|pourrais[-\s]tu|"
    r"c['’]est\s+possible|es[-\s]tu\s+capable|il\s+est\s+possible)\b"
    r"(?![^?]*[0-9@])"          # a figure or an e-mail means real data → not a capability question
    # //// Neoffice — two gaps this guard missed, both live on 2026-08-21 (voice
    # console): "Est-ce que tu peux m'en mettre quinze en commande ?" answered
    # "Oui, je peux gérer les commandes clients — quel produit, quel client ?"
    # while the previous turn had JUST identified the product and offered a
    # purchase order. Two tells make it an ORDER, not a capability question:
    # 1. a number spelled out — voice transcription writes figures as words, so
    #    the [0-9] guard never fires ("un/une" excluded: they are articles, and
    #    "peux-tu créer un client ?" must stay a capability question);
    # 2. an object pronoun (m'en, me le/la/les, nous en) — an anaphor points at
    #    something from the conversation, which a cold meta-question never does.
    r"(?![^?]*\b(?:deux|trois|quatre|cinq|six|sept|huit|neuf|dix|onze|douze|"
    r"treize|quatorze|quinze|seize|vingt|trente|quarante|cinquante|soixante|"
    r"cents?|mille)\b)"
    r"(?![^?]*\b(?:m['’]en|nous\s+en|t['’]en|me\s+l(?:e|a|es))\b)"
    # Asking for INFORMATION is not asking about a capability: "peux-tu me dire /
    # me donner / m'afficher …" is a real business request and must reach its pole.
    r"(?![^?]*\b(?:me\s+dire|me\s+donner|me\s+montrer|me\s+sortir|me\s+lister|"
    r"me\s+rappeler|me\s+trouver|me\s+chercher|m['’](?:afficher|indiquer|envoyer|donner|dire))\b)"
    r"[^?]{0,90}\?\s*$",
    re.IGNORECASE,
)

# //// Neoffice — explicit "yes, send it" confirmation (used ONLY on a support follow-up,
# see _fast_path). Matches a go-ahead — "oui", "vas-y", "envoie", "confirme", "ok envoie",
# "c'est bon envoyez" — but NOT a refusal: a leading negation ("non", "n'envoie pas",
# "pas maintenant") makes the whole match fail, so a "non" never triggers a send. The
# narrow scope (only when prior pole == support) keeps a bare "oui" elsewhere from being
# mis-treated as a send. grep "//// Neoffice".
_CONFIRM_SEND_RE = re.compile(
    r"^\s*(?!.*\b(?:non|pas|surtout pas|n['’]envoie|ne pas|annule|laisse tomber)\b)"
    r".*\b(?:oui|ouais|ok(?:ay)?|d['’]accord|daccord|vas[- ]?y|allez[- ]?y|go|"
    r"envoie[zs]?|envoy(?:er|ez)|confirme[zr]?|valide[zr]?|c['’]est bon|parfait|"
    r"feu vert|fais[- ]?le)\b",
    re.IGNORECASE,
)


def _fast_path(msg: str, prior: Optional[dict]) -> Optional[str]:
    """Unambiguous keyword → pole/'DIRECT' without an LLM call; else None (→ LLM classify).

    Never overrides the conversation-context (`prior`) or the 'recurrent' decision.
    """
    # //// Neoffice — a GO-AHEAD turn must DETERMINISTICALLY stay on the prior pole.
    # After turn 1 routes to a pole, that worker often PREPARES something and asks for
    # confirmation ("je vous prépare les relances ?", "j'envoie l'email ?"). Turn 2 is
    # the user's reply ("oui, envoie" / "vas-y" / "ok crée le rappel"). Without this,
    # that affirmative goes to the LLM classify and can drift OFF the pole — observed
    # 2026-06-30 (support email send never happened) and 2026-07-08 ("ok crée un rappel"
    # after a compta invoice list was classified 'recurrent' → declined → DIRECT → "je
    # ne sais pas quel rappel"). Originally support-only; generalized to EVERY pole.
    # Two guards keep it safe: only SHORT messages (a confirmation is short — long
    # messages carry new intent the LLM should read), and only when no OTHER pole's
    # keyword rule matches (an explicit "oui mais plutôt un graphique" must still
    # reach analyse). The SOUL still decides confirm vs re-draft; routing only
    # guarantees the right pole sees the confirmation. grep "//// Neoffice".
    if (
        prior
        and prior.get("pole") in POLES
        and len(msg) <= 80
        and _CONFIRM_SEND_RE.search(msg)
        and not _RECUR_RE.search(msg)
    ):
        _kw_pole = next((p for rx, p in _FAST_PATH_RULES if rx.search(msg)), None)
        if _kw_pole in (None, prior["pole"]):
            return prior["pole"]
    # //// END Neoffice ////
    # //// Neoffice — a capability question is META, whatever the conversation context.
    # It must be settled BEFORE the two paths below, and here is why: the follow-up rule
    # ("prior → keep the same pole") and the keyword fast-path each hijack it. Measured
    # end-to-end on osiris: "Est-ce que tu peux créer facilement un client ?" asked right
    # after an invoice question inherited compta→ventes, span a worker, and answered
    # "give me the name, email and address" after 21s — for a one-sentence question.
    # _CAPABILITY_RE is deliberately narrow: short question, no figure, no e-mail, so a
    # real order ("crée le client X, contact@exemple.ch") never reaches this branch and a
    # data request ("peux-tu me dire le montant…") is not a capability question either.
    if _CAPABILITY_RE.match(msg) and not _RECUR_RE.search(msg):
        logger.info("nora_chat_router: capability question → DIRECT (no pole, no worker)")
        return "DIRECT"
    # //// END Neoffice ////
    # //// Neoffice — a one-off reminder, see _ONE_OFF_REMINDER_RE above.
    if _is_one_off_reminder(msg):
        logger.info("nora_chat_router: one-off reminder → DIRECT (nora_reminder_create)")
        return "DIRECT"
    # //// END Neoffice ////
    # //// Neoffice — a follow-up still obeys an EXPLICIT rule. Until now ANY turn
    # //// with a prior pole skipped every deterministic rule and went to the LLM,
    # //// which is the one thing this module exists to avoid: the rules are
    # //// deterministic precisely because the classifier is not. Measured today on
    # //// a three-turn desk conversation — turn 1 "quel est le statut du chantier
    # //// PROJ-n" routed correctly, then "montre-moi l'apercu du devis de ce
    # //// chantier" drifted to the sales pole although the qualified-quote rule
    # //// matches it word for word. That pole holds no job tool, so its worker
    # //// looped and the customer read the guardrail's ENGLISH text.
    # //// The two scars this bail-out was built around are untouched: both were
    # //// SHORT confirmations ("oui envoie", "ok crée le rappel"), and the GO-AHEAD
    # //// rule above settles those before we ever get here. `recurrent` still
    # //// belongs to the LLM, so an explicit rule does not steal it.
    if prior and prior.get("pole"):
        if not _RECUR_RE.search(msg):
            _explicit = _keyword_pole(msg)
            if _explicit:
                logger.info(
                    "nora_chat_router: follow-up matched an explicit rule → %s "
                    "(prior=%s, classifier skipped)",
                    _explicit,
                    prior.get("pole"),
                )
                return _explicit
        return None  # nothing explicit → keep the context-aware LLM path
    # //// END Neoffice ////
    if _RECUR_RE.search(msg):
        return None  # recurring request → the LLM owns the 'recurrent' classification
    # //// Neoffice — one evaluation of the explicit rules, shared with the
    # //// follow-up branch above so both obey exactly the same table.
    _explicit = _keyword_pole(msg)
    if _explicit:
        return _explicit
    # //// END Neoffice ////
    if _DIRECT_RE.match(msg):
        return "DIRECT"
    return None


# //// Neoffice — SMALL-TALK LIGHT PATH gates. A greeting must never pay the full
# orchestrator agent (58 tool schemas of prefill = 17-30s for one sentence, measured
# 2026-07-09). STRICT double gate: an explicit small-talk cue AND no digits/business
# vocabulary — anything ambiguous keeps the normal agent path.
_SMALLTALK_RE = re.compile(
    r"\b(bonjour|salut|hello|hi|hey|coucou|bonsoir|merci|thanks|ça va|ca va|"
    r"tu vas bien|opérationnel(le)?|es[- ]tu (là|la)|t'es (là|la)|good (morning|evening)|"
    r"au revoir|bonne (journée|soirée|nuit)|comment vas)\b",
    re.IGNORECASE,
)
# //// Neoffice — the job vocabulary added (20.09). This is the ANTI-gate: a message
# //// that carries a greeting AND no business word takes the small-talk light path and
# //// is answered with one warm sentence. The list was written before `projet` existed,
# //// so it protected « Bonjour, ou en est ma facture ? » and not « Bonjour, on en est
# //// ou sur le chantier ? ». Measured that day, five job sentences out of six carrying
# //// a greeting passed straight through, while the same sentence about an invoice was
# //// correctly held back. The keyword rules catch most of them BEFORE this point, but
# //// this gate exists precisely for when they do not and the classifier falls back to
# //// DIRECT — which is exactly what an outage does (14.09, a whole day of fallbacks).
# //// Erring generous is right here: the failure is ASYMMETRIC. A false positive sends
# //// a greeting to the normal agent and costs latency; a false negative answers a real
# //// question with « Bonjour ! Comment puis-je vous aider ? ».
_BUSINESS_RE = re.compile(
    r"\d|\b(factur\w*|devis|client\w*|rappel\w*|relanc\w*|e-?mail\w*|command\w*|"
    r"article\w*|abonnement\w*|paiement\w*|stock\w*|rapport\w*|dunn\w*|briefing\w*|"
    r"fournisseur\w*|salaire\w*|employé\w*|ticket\w*|tâche\w*|tache\w*|"
    r"chantier\w*|visites?|m[ée]tr[ée]\w*|m[²³]|intervention\w*|atelier\w*|"
    r"entretien\w*|r[ée]paration\w*|planning\w*|tourn[ée]es?|heures?|mat[ée]riel\w*|"
    r"projets?)\b",
    re.IGNORECASE,
)
# //// END Neoffice ////
# //// Neoffice — answer "can you do X?" in one sentence, and say what you need to
# actually do it. This replaces a full worker round-trip (measured 21s) whose entire
# output was "give me the name, the e-mail and the address".
# //// Neoffice — the domain list now names the building-job side (20.09). It listed
# //// "quotes, invoices, clients, articles, payment reminders, emails, HR and charts"
# //// and stopped there, while `projet` became a pole of its own on 16.09 with
# //// seventeen write tools. A capability question is settled BEFORE the keyword rules,
# //// so every "tu peux ouvrir un chantier ?" landed here, on a prompt that had never
# //// heard of a chantier — and the model filled the hole. Measured on osiris against
# //// the live model, 20.09:
# ////   « Tu peux ouvrir un chantier ? »  → "j'ai besoin du nom du client, de la date de
# ////     début, du MONTANT ESTIMÉ OU DU DEVIS ASSOCIÉ, et de la description" — none of
# ////     which frappe_job_create takes.
# ////   « Peux-tu me faire un métré ? »   → "j'ai besoin des quantités, unités et PRIX
# ////     UNITAIRES" — exactly backwards: frappe_measure is given shapes and dimensions
# ////     and RETURNS the quantity. The answer asked the user for what the tool computes.
# //// And the refusal it produced for an out-of-domain question read the old list back
# //// to the customer verbatim — no chantiers, no visites, no métré.
# //// The "say yes" is now conditional on the domain. That half was already holding in
# //// practice (the same measurement: « Tu peux faire un virement bancaire ? » → « Non,
# //// je ne peux pas effectuer de virements bancaires »), but it held on the model's
# //// judgement rather than on anything written here, which is not a guarantee.
_CAPABILITY_SYSTEM = (
    "You are NORA, the Neoffice business assistant. The user asks whether you CAN do "
    "something. Answer in the user's language, in one or two sentences. "
    "Your domain: quotes, orders, invoices, clients, articles and stock, payment "
    "reminders, emails, HR, charts — and the whole building-job side: opening a job, "
    "its visits and appointments, its work lines, its take-off (surfaces, dimensions, "
    # //// Neoffice — the FRENCH product word is given, because the model translates
    # //// the English one literally and lands beside the ERP: measured 20.09, it
    # //// answered « contrats de maintenance » where every screen the customer reads
    # //// says « contrat d'entretien ». Naming a domain is not enough if the word
    # //// that comes back is not the one on the screen.
    "m²), its costing, the workshop, maintenance contracts (in French say « contrat "
    "d'entretien » — that is what the screens call it), and the hours and materials "
    "recorded on a job. "
    "If the request is in that domain, say yes, then list ONLY the information you need "
    "to actually do it: ask for what the USER knows (a customer, a date, dimensions), "
    "never for something the system works out by itself (a computed quantity, a total, "
    "a document id). If it is NOT in that domain, say so plainly and name what you do "
    "handle — never promise it. "
    "Do not perform the action, do not invent data or fields, do not mention tools or "
    "internals."
)
# //// END Neoffice ////

_SMALLTALK_SYSTEM = (
    "You are NORA, the Neoffice business assistant. Reply to this small-talk message "
    "briefly (one or two sentences), warm and professional, in the user's language. "
    "Plain text only — no lists, no tool talk, no task offers unless asked."
)

# //// Neoffice — CANNED SMALL-TALK (no LLM at all). The light path above still costs
# two serial LLM round-trips (classify, then a small-talk completion): 2.8 s for
# "Bonjour Nora" on a loaded Olares (measured 2026-09-01, 07:49:29.39 → 07:49:32.15),
# before the desk even polls. A greeting, a thank-you, a "how are you" or a farewell
# has one right answer per language; say it from a template. The gate is stricter
# than the light path's: the message must be small talk and NOTHING ELSE (residue
# after removing the cue and filler words ≤ 1 word), so "Bonjour, qui es-tu ?" still
# gets the LLM. Vouvoiement — customer-facing. Keep the four cue classes in step
# with _SMALLTALK_RE.
_CANNED_FAREWELL_RE = re.compile(
    r"\b(au revoir|bonne (journée|soirée|nuit)|à bientôt|a bientôt|bye|goodbye|"
    r"auf wiedersehen|schönen tag|arrivederci|buona giornata)\b", re.IGNORECASE
)
_CANNED_THANKS_RE = re.compile(r"\b(merci|thanks|thank you|danke|grazie)\b", re.IGNORECASE)
_CANNED_HOWAREYOU_RE = re.compile(
    r"\b(ça va|ca va|tu vas bien|vous allez bien|comment vas|comment allez|"
    r"opérationnel(le)?|es[- ]tu (là|la)|t'es (là|la)|how are you|wie geht|come va|come stai)\b",
    re.IGNORECASE,
)
_CANNED_EVENING_RE = re.compile(r"\b(bonsoir|good evening|guten abend|buonasera)\b", re.IGNORECASE)
_CANNED_REPLIES = {
    "fr": {
        "greeting": "Bonjour ! Comment puis-je vous aider aujourd'hui ?",
        "evening": "Bonsoir ! Comment puis-je vous aider ?",
        "thanks": "Avec plaisir ! N'hésitez pas si je peux faire autre chose pour vous.",
        "howareyou": "Très bien, merci — et vous ? Que puis-je faire pour vous ?",
        "farewell": "Bonne journée à vous, à bientôt !",
    },
    "de": {
        "greeting": "Guten Tag! Wie kann ich Ihnen heute helfen?",
        "evening": "Guten Abend! Wie kann ich Ihnen helfen?",
        "thanks": "Gern geschehen! Sagen Sie Bescheid, wenn ich noch etwas für Sie tun kann.",
        "howareyou": "Sehr gut, danke — und Ihnen? Was kann ich für Sie tun?",
        "farewell": "Einen schönen Tag noch, bis bald!",
    },
    "it": {
        "greeting": "Buongiorno! Come posso aiutarla oggi?",
        "evening": "Buonasera! Come posso aiutarla?",
        "thanks": "Con piacere! Mi dica pure se posso fare altro per lei.",
        "howareyou": "Molto bene, grazie — e lei? Cosa posso fare per lei?",
        "farewell": "Buona giornata, a presto!",
    },
    "en": {
        "greeting": "Hello! How can I help you today?",
        "evening": "Good evening! How can I help you?",
        "thanks": "You're welcome! Let me know if I can do anything else for you.",
        "howareyou": "Very well, thank you — and you? What can I do for you?",
        "farewell": "Have a good day, see you soon!",
    },
}
_CANNED_FILLER_RE = re.compile(
    r"\b(nora|et|vous|toi|tu|à|a|tous|toutes|bien|très|tres|moi|aussi|super|ok|d'accord|"
    r"beaucoup|bonne|good|hallo|ciao|you|and|there|the|everyone|all|dir|ihnen|lei)\b",
    re.IGNORECASE,
)


def _canned_smalltalk_reply(message: str, language: Optional[str]) -> Optional[str]:
    """Template reply for a message that is small talk and nothing else, else None."""
    msg = (message or "").strip()
    if not msg or len(msg) > 80:
        return None
    if _BUSINESS_RE.search(msg) or _CAPABILITY_RE.match(msg):
        return None
    if not (_SMALLTALK_RE.search(msg) or _CANNED_FAREWELL_RE.search(msg)
            or _CANNED_HOWAREYOU_RE.search(msg) or _CANNED_EVENING_RE.search(msg)):
        return None
    if _CANNED_FAREWELL_RE.search(msg):
        kind = "farewell"
    elif _CANNED_THANKS_RE.search(msg):
        kind = "thanks"
    elif _CANNED_HOWAREYOU_RE.search(msg):
        kind = "howareyou"
    elif _CANNED_EVENING_RE.search(msg):
        kind = "evening"
    else:
        kind = "greeting"
    # Residue check: strip every cue and filler word; anything substantive left
    # means the user asked something → not a canned case.
    residue = msg
    for rx in (_SMALLTALK_RE, _CANNED_FAREWELL_RE, _CANNED_THANKS_RE,
               _CANNED_HOWAREYOU_RE, _CANNED_EVENING_RE, _CANNED_FILLER_RE):
        residue = rx.sub(" ", residue)
    residue_words = [w for w in re.split(r"[^\w']+", residue) if w]
    if len(residue_words) > 1:
        return None
    return _CANNED_REPLIES[_norm_lang(language)][kind]
# //// END Neoffice ////
# //// END Neoffice ////


# //// Neoffice — the pole of the document on screen, used ONLY when the classifier fails
# //// and no earlier turn chose a pole. « Rajoute un siphon là-dessus » on a quotation got an
# //// empty verdict, went to the orchestrator and ended in « service indisponible » after
# //// 55 s (capability bench, 2026-09-24): the page said ventes all along.
_DOCTYPE_POLES = {
    "Quotation": "ventes", "Sales Order": "ventes", "Delivery Note": "ventes", "Customer": "ventes",
    "Item": "ventes", "Lead": "ventes", "Opportunity": "ventes",
    "Sales Invoice": "compta", "Purchase Invoice": "compta", "Payment Entry": "compta",
    "Journal Entry": "compta", "Purchase Order": "compta", "Supplier": "compta", "Dunning": "compta",
    "Employee": "rh", "Leave Application": "rh", "Expense Claim": "rh", "Salary Slip": "rh",
    "Project": "projet", "Task": "projet", "Timesheet": "projet",
    "Issue": "support", "HD Ticket": "support",
}
# //// END Neoffice ////


def classify(
    message: str,
    *,
    call_llm_fn: Callable[..., Any],
    main_runtime: Optional[dict],
    prior: Optional[dict] = None,
    timeout: float = 8.0,
    hint: Optional[str] = None,
    page_doctype: Optional[str] = None,  # //// Neoffice — failure fallback, see _DOCTYPE_POLES
) -> str:
    """Return a pole in :data:`POLES`, or ``"DIRECT"``.

    ``prior`` (optional ``{"msg", "pole"}``) is the previous turn's user message and the
    pole it routed to; when present it is fed to the classifier so a follow-up/refinement
    ("Ceux de ce client") stays on the same pole instead of being classified blind
    (a client name in a devis context would otherwise look like an RH/person query).

    Defensive by construction: an empty message, an LLM error/timeout, or an
    unrecognized reply all resolve to ``"DIRECT"`` so the caller falls back to the
    normal agent path (never drops a message because routing was uncertain).
    """
    msg = (message or "").strip()
    if not msg:
        return "DIRECT"
    # Latency: obvious messages skip the ~1s LLM call → the ack lands instantly.
    fast = _fast_path(msg, prior)
    if fast:
        logger.info("nora_chat_router: keyword fast-path → %s (no LLM call)", fast)
        return fast
    # //// Neoffice — the pole the calling page already chose (#681, 2026-09-24). The NORA
    # //// Live page escalates with the pole its voice server named, and that server has
    # //// ALREADY said it aloud (« je transmets aux ventes… »). Without the hint the
    # //// classifier decided again, and the ack could name one pole while another worked.
    # //// After the deterministic rules, which stay the only thing above the page's word.
    if hint in POLES:
        logger.info("nora_chat_router: page pole hint → %s (no LLM call)", hint)
        return hint
    # //// END Neoffice ////
    user_content = msg[:2000]
    if prior and prior.get("pole"):
        user_content = (
            f"[Contexte conversation — message précédent : « {(prior.get('msg') or '')[:200]} » "
            f"→ pôle « {prior['pole']} ». Si ce nouveau message est une suite/précision de ce "
            f"qui précède, garde le pôle « {prior['pole']} ».]\n{user_content}"
        )
    messages = [
        {"role": "system", "content": _CLASSIFIER_SYSTEM},
        {"role": "user", "content": user_content},
    ]
    try:
        resp = call_llm_fn(
            task="nora_chat_routing",
            messages=messages,
            max_tokens=8,
            temperature=0.0,
            timeout=timeout,
            main_runtime=main_runtime,
        )
        raw = (resp.choices[0].message.content or "").strip().lower()
        # //// Neoffice — an EMPTY verdict is a failure, not « direct »: asked once more,
        # //// then the same fallback as an exception. On 24.09 at 21:17 raw='' sent a request
        # //// made on a quotation page to the orchestrator, which ran its own card and
        # //// answered « je n'ai pas réussi » after 76 s (capability bench, 1 in 35 calls).
        if not raw:
            resp = call_llm_fn(
                task="nora_chat_routing",
                messages=messages,
                max_tokens=8,
                temperature=0.0,
                timeout=timeout,
                main_runtime=main_runtime,
            )
            raw = (resp.choices[0].message.content or "").strip().lower()
            if not raw:
                raise RuntimeError("the classifier answered nothing, twice")
        # //// END Neoffice ////
    except Exception as exc:  # noqa: BLE001 — any failure must degrade, never raise
        # //// Neoffice — degrade to the PRIOR pole, not to DIRECT (17.09). The classifier
        # //// gets eight seconds; when the model is busy it does not answer in time, and
        # //// the log shows this path taken 33 times. Falling to DIRECT hands the turn to
        # //// the gateway agent, which holds not one pole tool — so a thread already on
        # //// compta answered its follow-up with nothing. Staying put is strictly better:
        # //// the prior pole was chosen by a classification that DID succeed. With no
        # //// prior, DIRECT remains the safe default (never drop a message).
        fallback = (prior or {}).get("pole")
        if fallback in POLES:
            logger.warning(
                "nora_chat_router: classifier failed, staying on prior pole %s: %s",
                fallback,
                exc,
            )
            return fallback
        # //// Neoffice — no prior pole: the document on screen decides, see _DOCTYPE_POLES.
        page_pole = _DOCTYPE_POLES.get(page_doctype or "")
        if page_pole in POLES:
            logger.warning("nora_chat_router: classifier failed, pole of the page's %s → %s: %s",
                           page_doctype, page_pole, exc)
            return page_pole
        # //// END Neoffice ////
        logger.warning("nora_chat_router: classifier failed, falling back to DIRECT: %s", exc)
        return "DIRECT"
    # //// Neoffice — the verdict is logged (05.09): a job-page write went DIRECT with no
    # line in the log, and nothing said whether Olares answered 'direct' or nothing. ////
    logger.info("nora_chat_router: classifier raw=%r", raw[:80])
    tokens = _TOKEN_RE.findall(raw)
    for tok in tokens:
        if tok == "recurrent":
            return "recurrent"
        if tok in POLES:
            return tok
        if tok in ("direct", "aucun", "none"):
            return "DIRECT"
    # Unrecognized output → safest is to let the agent handle it.
    logger.info("nora_chat_router: unrecognized classifier output %r → DIRECT", raw[:80])
    return "DIRECT"


def build_ack(pole: str, language: Optional[str] = None) -> str:
    """Acknowledgment naming the business pole, in the user's language (default French)."""
    # //// Neoffice — language-aware ack (was French-only) ////
    lang = _norm_lang(language)
    labels = POLE_LABELS.get(lang, POLE_LABELS["fr"])
    label = labels.get(pole) or POLE_LABELS["fr"].get(pole, "support")
    return ACK_TEMPLATES.get(lang, ACK_TEMPLATES["fr"]).format(label=label)
    # //// END Neoffice ////


def _add_notify_sub(notify_db, conn, *, conversation_id: Optional[str] = None, **kw) -> None:
    """Subscribe the notifier so the worker's terminal result comes back to THIS chat.

    The desk conversation id rides in upstream's ``delivery_metadata`` (a JSON blob the
    notifier copies verbatim into the adapter's send metadata), so no fork column is
    needed: ``webhook.send`` reads ``metadata["conversation_id"]`` to reach the right
    desk thread. Before v2026.9.7 we carried a bespoke ``conversation_id`` column here.
    """
    if conversation_id:
        metadata = dict(kw.pop("delivery_metadata", None) or {})
        metadata.setdefault("conversation_id", conversation_id)
        kw["delivery_metadata"] = metadata
    notify_db.add_notify_sub(conn, **kw)


def _post_ack_to_callback(ack: str, deliver_extra: Optional[dict]) -> bool:
    """Deliver the immediate ack to the desk via the nora callback — the SAME POST the
    nora-deliver path uses ({conversation_id, text} + X-Hermes-Token). We POST it here
    rather than via the platform's ``_direct_deliver`` because that routes the custom
    "nora" deliver type to ``_deliver_cross_platform``, which doesn't know it, so the
    ack was silently dropped (success=False, no exception). Desk only — when there is no
    callback_url (e.g. WhatsApp) we skip; the worker's result still delivers via the
    notifier. Never raises: a failed ack must not break routing."""
    extra = deliver_extra or {}
    url = (extra.get("callback_url") or "").strip()
    token = (extra.get("callback_token") or "").strip()
    cid = (extra.get("conversation_id") or "").strip()
    if not (url and token and cid and ack):
        return False
    import json as _json
    import urllib.request

    body = _json.dumps({"conversation_id": cid, "text": ack}).encode()
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"X-Hermes-Token": token, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            status = getattr(resp, "status", 0)
            # //// Neoffice — the thread each ack / fast answer went to (24.09: two replies
            # //// delivered with a 200 never showed in the chat; only final replies named it).
            logger.info("nora_chat_router: posted to cid=%s status=%s (%d chars)", cid, status, len(ack))
            # //// END Neoffice ////
            return 200 <= status < 300
    except Exception as exc:  # noqa: BLE001
        logger.warning("nora_chat_router: ack callback POST failed: %s", exc)
        return False


def _route_recurrent(
    message: str, chat_user: Optional[str], deliver_extra: Optional[dict]
) -> dict:
    """RECURRENT class: a recurring request ("relève mes mails tous les matins"). We create
    the scheduled task IN CODE via the nora ``task_router.route_recurrent`` endpoint — it
    extracts the schedule DETERMINISTICALLY (time/frequency/kind/mailbox), scopes it to the
    user, and returns a fixed ack we deliver to the desk. Like the kanban path, this takes
    the action out of the weak model's hands. Any failure (no user, endpoint down, declined)
    → routed=False so the caller falls back to the normal agent dispatch (zero regression).
    The Frappe URL + auth are derived from the SAME desk callback the ack POST uses."""
    extra = deliver_extra or {}
    cb = (extra.get("callback_url") or "").strip()
    token = (extra.get("callback_token") or "").strip()
    cid = (extra.get("conversation_id") or "").strip()
    user = (chat_user or "").strip()
    _DELIVER = "nora.api.v2.hermes_callback.deliver"
    _ROUTE = "nora.api.v2.task_router.route_recurrent"
    if not (cb and token and user) or _DELIVER not in cb:
        return {"routed": False, "category": "recurrent", "ack": None, "task_id": None}
    import json as _json
    import urllib.request

    url = cb.replace(_DELIVER, _ROUTE)
    body = _json.dumps({"user": user, "message": message, "conversation_id": cid}).encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"X-Hermes-Token": token, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = _json.loads(resp.read().decode() or "{}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("nora_chat_router: recurrent POST failed → agent fallback: %s", exc)
        return {"routed": False, "category": "recurrent", "ack": None, "task_id": None}
    # //// Neoffice — a whitelisted Frappe method answers {"message": {...}}. This read "ok"
    # //// on the envelope, so every recurring request was declined and fell to the agent
    # //// (dev-instance gateway log: « recurrent declined (None) », never a task).
    if isinstance(data, dict) and isinstance(data.get("message"), dict):
        data = data["message"]
    # //// END Neoffice ////
    if not isinstance(data, dict) or not data.get("ok"):
        logger.info(
            "nora_chat_router: recurrent declined (%s) → agent fallback", (data or {}).get("error")
        )
        # //// Neoffice — `reason` tells an unsupported cadence from none, see the caller.
        return {"routed": False, "category": "recurrent", "ack": None, "task_id": None,
                "reason": (data or {}).get("reason") if isinstance(data, dict) else None}
    ack = data.get("ack") or "C'est noté, je programme ça."
    ack_delivered = _post_ack_to_callback(ack, deliver_extra)
    logger.info(
        "nora_chat_router: recurrent task=%s ack_delivered=%s", data.get("task_id"), ack_delivered
    )
    return {
        "routed": True, "category": "recurrent", "ack": ack,
        "task_id": data.get("task_id"), "ack_delivered": ack_delivered,
    }


# //// Neoffice — a one-off reminder is SET IN CODE by nora (task_router.route_reminder),
# //// over the same desk callback and token as _route_recurrent. Routing it to the agent
# //// was not enough: the model delegated it, then repeated its own earlier « C'est noté »
# //// found in memory, with no reminder written (dev instance, 2026-09-24). Declined or
# //// failed → routed=False and the agent path keeps the instruction (zero regression).
def _route_reminder(message: str, chat_user: Optional[str], deliver_extra: Optional[dict]) -> dict:
    extra = deliver_extra or {}
    cb = (extra.get("callback_url") or "").strip()
    token = (extra.get("callback_token") or "").strip()
    user = (chat_user or "").strip()
    _DELIVER = "nora.api.v2.hermes_callback.deliver"
    _ROUTE = "nora.api.v2.task_router.route_reminder"
    declined = {"routed": False, "category": "DIRECT", "ack": None, "task_id": None}
    if not (cb and token and user) or _DELIVER not in cb:
        return declined
    import json as _json
    import urllib.request

    req = urllib.request.Request(
        cb.replace(_DELIVER, _ROUTE),
        data=_json.dumps({"user": user, "message": message,
                          "conversation_id": (extra.get("conversation_id") or "").strip()}).encode(),
        method="POST", headers={"X-Hermes-Token": token, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = _json.loads(resp.read().decode() or "{}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("nora_chat_router: reminder POST failed → agent fallback: %s", exc)
        return declined
    if isinstance(data, dict) and isinstance(data.get("message"), dict):
        data = data["message"]  # a whitelisted Frappe method answers {"message": {...}}
    if not isinstance(data, dict) or not data.get("ok") or not data.get("ack"):
        logger.info("nora_chat_router: reminder declined (%s) → agent fallback", (data or {}).get("error"))
        return declined
    delivered = _post_ack_to_callback(data["ack"], deliver_extra)
    logger.info("nora_chat_router: one-off reminder %s set in code, ack_delivered=%s", data.get("reminder"), delivered)
    return {"routed": True, "category": "DIRECT", "ack": data["ack"], "task_id": None,
            "reminder": data.get("reminder"), "ack_delivered": delivered}
# //// END Neoffice ////


# //// Neoffice — DETERMINISTIC FAST-PATH engine call. Ask the nora fast_answer engine
# (ONE structured-intent LLM call + a Frappe query in CODE) over the SAME desk callback
# the ack uses (X-Hermes-Token), exactly like _route_recurrent. Returns the answer text
# on a confident hit, else None → the caller routes to a worker (zero regression).
# grep "//// Neoffice".
def _fast_answer(
    message: str,
    chat_user: Optional[str],
    deliver_extra: Optional[dict],
    context: str = "",
) -> Optional[str]:
    extra = deliver_extra or {}
    cb = (extra.get("callback_url") or "").strip()
    token = (extra.get("callback_token") or "").strip()
    user = (chat_user or "").strip()
    _DELIVER = "nora.api.v2.hermes_callback.deliver"
    _FAST = "nora.api.fast_answer.answer_gateway"
    if not (cb and token and user) or _DELIVER not in cb:
        return None
    import json as _json
    import urllib.request

    url = cb.replace(_DELIVER, _FAST)
    # //// Neoffice — carry the conversation film. Without it the engine cannot
    # resolve a follow-up referent ("et le CA de CE client ?"): the model returns an
    # empty filter, the fast path declines, and the question costs a 16-19 s worker
    # for something answerable in under a second. The film sits right here in the
    # router; not passing it was the whole gap (measured 2026-08-11).
    payload = {"user": user, "message": message}
    if (context or "").strip():
        payload["context"] = context.strip()[:1500]
    # //// Neoffice — the conversation id keys the PAGE context on the desk side
    # (nora_page_context:<cid>): the docked job panel stores the project there, and
    # « on en est où sur ce chantier ? » is answered in code from it (05.09).
    if (extra.get("conversation_id") or "").strip():
        payload["conversation_id"] = str(extra.get("conversation_id")).strip()[:64]
    body = _json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"X-Hermes-Token": token, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = _json.loads(resp.read().decode() or "{}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("nora_chat_router: fast_answer POST failed → worker: %s", exc)
        return None
    # Frappe wraps a whitelisted method's return value in {"message": ...} — unwrap it.
    if isinstance(data, dict) and isinstance(data.get("message"), dict):
        data = data["message"]
    if isinstance(data, dict) and data.get("answered") and (data.get("text") or "").strip():
        logger.info("nora_chat_router: FAST-ANSWER hit (%d chars)", len(data["text"]))
        return data["text"].strip()
    return None
# //// END Neoffice ////


# //// Neoffice — the job the desk page names (docked job panel, 05.09).
# //// Neoffice — the DOCUMENT the user is looking at, for every pole. Only a job page was
# //// anchored: on a customer's form « Modifie l'adresse de ce client » reached ventes with
# //// no customer, the worker asked for the name, and the « Oui, vas-y » that followed had
# //// it pick a customer itself and change its address; on a quotation, « Mets 10 % sur ce
# //// produit » ended with the quotation SUBMITTED and its acceptance link sent (capability
# //// bench, 2026-09-24). compta coped only because its SOUL calls get_page_context.
def document_page_anchor(page_context: Optional[dict]) -> Optional[str]:
    """« [Page context — the user is looking at the Customer « Boulangerie X » (CUST-0001)…] »,
    or None on a list, on no page, or on a job (job_page_anchor has its own)."""
    pc = page_context if isinstance(page_context, dict) else {}
    doctype = " ".join(str(pc.get("doctype") or "").split())[:60]
    name = " ".join(str(pc.get("name") or "").split())[:140]
    if not (doctype and name) or doctype == "Project":
        return None
    title = " ".join(str(pc.get("title") or "").split())[:140]
    label = f"« {title} » ({name})" if title and title != name else name
    return (f"[Page context — the user is looking at the {doctype} {label}. « ce / cette / cet … » "
            "(this customer, this quotation, this article…) means THIS document unless the message "
            "names another one. Never pick another document yourself.]")
# //// END Neoffice ////


# //// Neoffice — « ce produit » that nothing names. With no page, no earlier turn and no
# //// number, a ventes worker asked « Mets du 10 % sur ce produit » tried invented document
# //// numbers (FA-2026-01062, CMD-0031, DEV-0042) five times each until the loop guard, instead
# //// of asking which one (dev instance, 2026-09-24 22:32; nothing was written).
_DEICTIC_NOUNS = (
    r"produit|article|client|cliente|fournisseur|devis|offre|facture|commande|bon|document|ligne|"
    r"contact|adresse|chantier|projet|ticket|employ[ée]e?|collaborat\w+|dossier|paiement|"
    r"product|item|customer|supplier|quote|quotation|invoice|order|line|address|job|project|employee|"
    r"produkt|artikel|kunde|kundin|lieferant|angebot|offerte|rechnung|bestellung|auftrag|dokument|"
    r"prodotto|articolo|fornitore|preventivo|offerta|fattura|ordine|documento"
)
# Case-insensitive for the words only: the look-ahead's capital means « a name follows ».
_DEICTIC_UNNAMED_RE = re.compile(
    rf"\b(?i:(?:ce|cet|cette|this|dies(?:e[rsmn]?)?|quest[oa])\s+(?:{_DEICTIC_NOUNS}))\b"
    r"(?!\s*(?:[A-ZÀ-Ý]|\d|«|\"|'))",
)
_DOC_ID_RE = re.compile(r"\b[A-Z][A-Z0-9]*-[A-Z0-9-]*\d")
UNNAMED_DOCUMENT_HINT = (
    "[The request says « this … » (ce / cette …), but no page is open, no earlier turn names it and "
    "no number is given. Do NOT guess document names or numbers and do NOT try candidates: ask the "
    "user which one (kanban_block kind=needs_input) and STOP.]"
)


def unnamed_document_hint(message: str, page_context: Optional[dict], film: list) -> Optional[str]:
    """UNNAMED_DOCUMENT_HINT when « ce produit » refers to nothing the worker can know."""
    if film or document_page_anchor(page_context) or _page_project(page_context):
        return None
    text = str(message or "")
    if _DOC_ID_RE.search(text) or not _DEICTIC_UNNAMED_RE.search(text):
        return None
    return UNNAMED_DOCUMENT_HINT
# //// END Neoffice ////


# //// Neoffice — « passe les mitigeurs à quatre pièces » is a QUANTITY. A ventes worker read
# //// « mitigeur 4 pièces » as an article's name: it had the quotation's line in front of it,
# //// searched articles « quatre pieces », « mitigeur lavabo 4 pièces »… until the loop guard,
# //// and answered that it could not (capability bench, 2026-09-24 23:49). Only a count WITH
# //// its unit word, in a request that changes something: « mets le prix à 95 », « 5 % de
# //// remise » and « commande 4 pièces » stay untouched.
_NUMBER_WORDS = {
    "un": 1, "une": 1, "deux": 2, "trois": 3, "quatre": 4, "cinq": 5, "six": 6, "sept": 7,
    "huit": 8, "neuf": 9, "dix": 10, "onze": 11, "douze": 12, "quinze": 15, "vingt": 20,
    "ein": 1, "eine": 1, "einen": 1, "zwei": 2, "drei": 3, "vier": 4, "fünf": 5, "sechs": 6,
    "sieben": 7, "acht": 8, "neun": 9, "zehn": 10, "zwölf": 12, "zwanzig": 20,
    "uno": 1, "due": 2, "tre": 3, "quattro": 4, "cinque": 5, "sette": 7, "otto": 8,
    "nove": 9, "dieci": 10, "venti": 20,
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "twelve": 12, "twenty": 20,
}
_COUNT = r"(?P<n>\d+(?:[.,]\d+)?|" + "|".join(sorted(_NUMBER_WORDS, key=len, reverse=True)) + r")"
_QTY_UNIT = r"(?:pi[eè]ces?|pces?|pcs|unit[ée]s|unit[ée]|exemplaires?|st(?:ü|ue)ck|stk|pezz[io]|unit[àa]|pieces?|units?)"
_QTY_COUNT_RE = re.compile(rf"(?i)\b{_COUNT}\s+{_QTY_UNIT}\b")
# « la quantité des mitigeurs à 4 »: the count follows « à / auf / to », never « d'une ligne ».
_QTY_WORD_RE = re.compile(
    rf"(?i)\b(?:quantit[ée]|qt[ée]|menge|anzahl|quantit[àa]|quantity|qty)\b[^\d\n]{{0,40}}?"
    rf"(?:\s|^)(?:à|a|auf|su|to|=|:)\s*{_COUNT}\b"
)
_QTY_EDIT_RE = re.compile(
    r"(?i)\b(?:pass(?:e|er|ez)|met(?:s|tre|tez)|chang(?:e|er|ez)|modifi(?:e|er|ez)|augment(?:e|er|ez)|"
    r"diminu(?:e|er|ez)|r[ée]dui(?:s|re|sez)|port(?:e|er|ez)|ajust(?:e|er|ez)|corrig(?:e|er|ez)|"
    r"au lieu d|plut[ôo]t que|[äa]nder\w*|setz\w*|erh[öo]h\w*|reduzier\w*|statt|anstatt|cambi\w*|"
    r"modific\w*|aument\w*|ridu\w*|mett\w*|port[ai]\w*|invece di|set|increase\w*|reduce\w*|instead of)\b"
)


def quantity_change_hint(message: str) -> Optional[str]:
    """A hint that « à quatre pièces » is a quantity, when a request changes a count of articles."""
    text = str(message or "")
    if not _QTY_EDIT_RE.search(text):
        return None
    m = _QTY_COUNT_RE.search(text) or _QTY_WORD_RE.search(text)
    if not m:
        return None
    raw = m.group("n")
    n = _NUMBER_WORDS.get(raw.lower(), raw)
    return (
        f"[« {m.group(0).strip()} » is a QUANTITY ({n}) of an article, not part of the article's "
        "name: never search for an article whose name contains the number. On a document that "
        f"already has that article, change the quantity of its line (frappe_draft_line "
        f"action=update, qty={n}).]"
    )
# //// END Neoffice ////


# //// Neoffice — a question asked ALOUD is answered aloud (NORA Live, #759). The voice
# //// clients (the NORA Live page and the quick chat's live widget) read the worker's reply
# //// through speech synthesis and keep only its prose sentences: a table or a list is shown,
# //// never spoken. A reply that opens on a table or a list therefore leaves nothing, or only
# //// a preamble, to say before the answer. « nora-live-widget » is the widget's own tag when
# //// its page names none; a message TYPED on the NORA Live page carries « nora-console » and
# //// keeps the usual form.
VOICE_SOURCES = frozenset({"nora-console-voice", "nora-quick-voice", "nora-live-widget"})
VOICE_ANSWER_HINT = (
    "[The question was asked aloud and the answer will be read by speech synthesis. Start with "
    "the answer itself in one or two short sentences (the figure, the name, the result), with no "
    "table, list or markdown before it. Details may follow after those sentences.]"
)


def voice_answer_hint(page_context: Optional[dict]) -> Optional[str]:
    """VOICE_ANSWER_HINT when the turn comes from a voice client, else None."""
    pc = page_context if isinstance(page_context, dict) else {}
    return VOICE_ANSWER_HINT if str(pc.get("source") or "").strip() in VOICE_SOURCES else None
# //// END Neoffice ////


def _page_project(page_context: Optional[dict]) -> Optional[str]:
    pc = page_context if isinstance(page_context, dict) else {}
    job = pc.get("job") if isinstance(pc.get("job"), dict) else {}
    return (
        pc.get("project") or job.get("project")
        or (pc.get("name") if pc.get("doctype") == "Project" else None)
    ) or None
# //// END Neoffice ////


# //// Neoffice — the classifier and the light-path completion are LLM calls made from the
# //// webhook handler, outside any turn: they need the launch profile's secrets (v2026.9.24).
@with_launch_profile_secrets
def route_chat_message(
    *,
    message: str,
    session_chat_id: str,
    conversation_id: Optional[str],
    thread_id: Optional[str],
    user_id: Optional[str],
    notifier_profile: Optional[str],
    idempotency_key: Optional[str],
    call_llm_fn: Callable[..., Any],
    main_runtime: Optional[dict],
    deliver_extra: Optional[dict] = None,
    chat_user: Optional[str] = None,
    board: Optional[str] = None,
    classify_timeout: float = 8.0,
    language: Optional[str] = None,  # //// Neoffice — user's response language (multilingual) ////
    chat_phone: Optional[str] = None,  # //// Neoffice — phone, to match a pending briefing offer ////
    page_context: Optional[dict] = None,  # //// Neoffice — the desk page the user is on (job panel) ////
) -> dict:
    """Classify *message* and, when it is a business request, create the kanban task
    in code + subscribe the notifier. Returns a decision dict::

        {"routed": bool, "category": str, "ack": Optional[str], "task_id": Optional[str]}

    ``routed=False`` (category ``DIRECT`` or any creation failure) means the caller
    MUST proceed with the normal agent dispatch — the message is never dropped.
    """
    prior = _LAST_ROUTE.get(conversation_id) if conversation_id else None

    # //// Neoffice — briefing CTA continuity. On the FIRST inbound after an out-of-band
    # briefing (no in-memory prior yet), an AFFIRMATIVE reply to NORA's recorded offer is
    # routed straight to the offer's pole, with the offered action injected as context
    # (below) so "oui" leads to the action instead of a blind DIRECT greeting. A negative
    # reply just consumes the offer; anything else falls through to normal classification.
    # grep "//// Neoffice".
    _offer = None
    if prior is None and chat_phone:
        _cand = _read_pending_offer(chat_phone)
        if _cand:
            if _AFFIRM_RE.search(message or ""):
                _offer = _cand
                _consume_pending_offer(chat_phone)
            elif _NEGATE_RE.search(message or ""):
                _consume_pending_offer(chat_phone)
    # //// END Neoffice ////

    # //// Neoffice — pure small talk is answered from a template: no classifier, no
    # fast-answer thread, no light-path completion (see _canned_smalltalk_reply).
    _canned_text = _canned_smalltalk_reply(message, language) if _offer is None else None
    # //// END Neoffice ////

    # //// Neoffice — PARALLEL classify + fast-answer. They were serial (two
    # LLM round-trips ≈ 1.9 s before any work started, on EVERY path). They
    # are independent — the fast-answer engine classifies its own intent —
    # so run both at once and keep the same priority when joining:
    # recurrent > fast-hit > pole routing > DIRECT. On greetings the
    # fast-answer pre-gate fails in ~0 ms without an LLM call, so the extra
    # thread costs nothing there.
    _fa_future = None
    # //// Neoffice — a one-off reminder is an action nora sets in code (_route_reminder):
    # //// the read-only fast-answer engine has nothing to answer, so it is not asked.
    _one_off_reminder = _is_one_off_reminder(message)
    if _norm_lang(language) == "fr" and not _canned_text and not _one_off_reminder:
        import concurrent.futures as _cf

        _fa_pool = _cf.ThreadPoolExecutor(max_workers=1)
        _fa_film = "\n".join(_CONV_HISTORY.get(conversation_id) or []) if conversation_id else ""
        _fa_future = _fa_pool.submit(
            _fast_answer, message, chat_user, deliver_extra, _fa_film
        )
        _fa_pool.shutdown(wait=False)
    # //// END Neoffice ////

    if _offer:
        category = _offer["pole"]  # //// Neoffice — pole fixed by the accepted offer ////
    elif _canned_text:
        category = "DIRECT"  # //// Neoffice — canned small talk, classifier skipped ////
    else:
        category = classify(
            message,
            call_llm_fn=call_llm_fn,
            main_runtime=main_runtime,
            prior=prior,
            timeout=classify_timeout,
            hint=(page_context or {}).get("pole_hint") if isinstance(page_context, dict) else None,  # //// Neoffice — see classify ////
            page_doctype=((page_context or {}).get("doctype") if isinstance(page_context, dict)  # //// Neoffice
                          and (page_context or {}).get("name") else None),
        )
    # //// Neoffice — on a job page, a business request is the job's (05.09). « Ajoute 2
    # heures de pose sur ce chantier » classified as RH (« heures ») and the RH worker,
    # which has no job tools, edited ANOTHER project's line with generic tools. The
    # `projet` pole owns the job tools (moved out of ventes on 16.09); compta/analyse
    # keep money and charts questions.
    if category in ("rh", "support") and _page_project(page_context):
        logger.info("nora_chat_router: %s → projet (job page %s)", category, _page_project(page_context))
        category = "projet"
    # //// END Neoffice ////
    # //// Neoffice — on a job page, DIRECT is only for small talk (05.09). « Ajoute sur ce
    # chantier l'ouvrage … » came back DIRECT (a 'direct' verdict, or no verdict at all on a
    # freshly restarted gateway whose auxiliary runtime is unset until the first agent run):
    # the orchestrator then wrote its own task body WITHOUT the page context, and the ventes
    # worker guessed the job from keywords (« chambre », « peinture »). A business sentence
    # on a job page belongs to projet. Canned small talk and the capability question were
    # settled before classify; a greeting-shaped message stays DIRECT.
    if (
        category == "DIRECT"
        and not _canned_text
        and _page_project(page_context)
        and not _DIRECT_RE.match((message or "").strip())
        and not _CAPABILITY_RE.match((message or "").strip())
    ):
        logger.info("nora_chat_router: DIRECT → projet (job page %s)", _page_project(page_context))
        category = "projet"
    # //// END Neoffice ////
    # Remember this turn so the NEXT message resolves a follow-up in context. Track DIRECT
    # too (pole=None) so a follow-up to a greeting doesn't inherit a stale pole.
    if conversation_id:
        if len(_LAST_ROUTE) > _LAST_ROUTE_MAX:
            _LAST_ROUTE.clear()
        _LAST_ROUTE[conversation_id] = {
            "msg": (message or "")[:200],
            "pole": (category if category in POLES else None),
        }
        # //// Neoffice — accumulate the rolling conversation film (INCLUDING DIRECT turns,
        # which carry the goal, e.g. the opening "créer un abonnement"). "User:"-prefixed;
        # NORA's delivered replies enter via note_nora_reply(). grep "//// Neoffice".
        if len(_CONV_HISTORY) > _LAST_ROUTE_MAX:
            _CONV_HISTORY.clear()
        _conv_film = _CONV_HISTORY.setdefault(conversation_id, [])
        _conv_film.append("User: " + (message or "")[:240])
        del _conv_film[:-_CONV_HISTORY_TURNS]
        # //// END Neoffice ////
    if category == "DIRECT":
        # //// Neoffice — a one-off reminder is set in code first, see _route_reminder.
        # //// Declined or failed: straight to the orchestrator with the instruction,
        # //// which can ask for the moment or call nora_reminder_create itself.
        if _one_off_reminder:
            _rem = _route_reminder(message, chat_user, deliver_extra)
            if _rem.get("routed"):
                note_nora_reply(conversation_id, _rem["ack"])
                return _rem
            return {"routed": False, "category": "DIRECT", "ack": None, "task_id": None,
                    "agent_hint": _ONE_OFF_REMINDER_HINT}
        # //// END Neoffice ////
        # //// Neoffice — a deterministic fast hit beats the light path (05.09). A job
        # status question (« on en est où sur ce chantier ? ») classifies as DIRECT, and
        # the join that delivers fast hits sits AFTER this branch's returns: the hit was
        # computed in parallel and thrown away while the agent answered slowly or wrongly.
        # grep "//// Neoffice".
        if _fa_future is not None:
            try:
                _fa_hit = _fa_future.result(timeout=12)
            except Exception as _fa_exc:  # noqa: BLE001 — engine crash = light path as before
                logger.warning("nora_chat_router: fast_answer (DIRECT) failed: %s", _fa_exc)
                _fa_hit = None
            _fa_future = None  # joined once; the later block must not wait again
            if _fa_hit:
                _fa_cid = ((deliver_extra or {}).get("conversation_id") or conversation_id or "")
                _fa_done = _post_ack_to_callback(_fa_hit, {**(deliver_extra or {}), "conversation_id": _fa_cid})
                note_nora_reply(conversation_id, _fa_hit)
                logger.info("nora_chat_router: FAST-ANSWER chat=%s → DIRECT delivered=%s", session_chat_id, _fa_done)
                return {
                    "routed": True, "category": "DIRECT", "ack": _fa_hit,
                    "task_id": None, "fast": True, "ack_delivered": _fa_done,
                }
        # //// END Neoffice ////
        # //// Neoffice — SMALL-TALK LIGHT PATH: one lightweight LLM call (same aux
        # client as the classifier, no agent, no tools) delivered directly. Any
        # failure or gate miss falls through to the normal agent (zero regression).
        # //// Neoffice — a capability question takes this light path too. Routing it to
        # DIRECT is not enough: the orchestrator still delegates to a pole on its own
        # (measured on osiris — 17s to decide, then a 21s worker, to answer "give me the
        # name and e-mail"). Answering it here costs one short LLM call. The _BUSINESS_RE
        # gate is deliberately skipped: a capability question NAMES a business object
        # ("créer un client") without carrying any data — that is the whole point.
        # //// Neoffice — canned small talk: deliver the template exactly like the light
        # path does (same callback, same film bookkeeping), zero LLM calls.
        if _canned_text:
            _cn_cid = (deliver_extra or {}).get("conversation_id") or (conversation_id or None)
            _cn_delivered = _post_ack_to_callback(
                _canned_text, {**(deliver_extra or {}), "conversation_id": _cn_cid}
            )
            note_nora_reply(conversation_id, _canned_text)
            logger.info(
                "nora_chat_router: SMALLTALK canned (no LLM, %d chars) delivered=%s",
                len(_canned_text), _cn_delivered,
            )
            return {
                "routed": True, "category": "DIRECT", "ack": _canned_text,
                "task_id": None, "fast": True, "ack_delivered": _cn_delivered,
            }
        # //// END Neoffice ////
        _is_capability = bool(_CAPABILITY_RE.match(message or ""))
        if _is_capability or (
            len(message or "") <= 80
            and _SMALLTALK_RE.search(message or "")
            and not _BUSINESS_RE.search(message or "")
        ):
            try:
                _lp_resp = call_llm_fn(
                    task="nora_capability" if _is_capability else "nora_smalltalk",
                    messages=[
                        {"role": "system",
                         "content": _CAPABILITY_SYSTEM if _is_capability else _SMALLTALK_SYSTEM},
                        {"role": "user", "content": (message or "")[:300]},
                    ],
                    max_tokens=120,
                    temperature=0.4,
                    timeout=10,
                    main_runtime=main_runtime,
                )
                _lp_text = (_lp_resp.choices[0].message.content or "").strip()
                if _lp_text:
                    _lp_cid = (deliver_extra or {}).get("conversation_id") or (conversation_id or None)
                    _lp_delivered = _post_ack_to_callback(
                        _lp_text, {**(deliver_extra or {}), "conversation_id": _lp_cid}
                    )
                    note_nora_reply(conversation_id, _lp_text)
                    logger.info(
                        "nora_chat_router: SMALLTALK light path (%d chars) delivered=%s",
                        len(_lp_text), _lp_delivered,
                    )
                    return {
                        "routed": True, "category": "DIRECT", "ack": _lp_text,
                        "task_id": None, "fast": True, "ack_delivered": _lp_delivered,
                    }
            except Exception as _lp_exc:  # noqa: BLE001 — degrade to the agent path
                logger.warning("nora_chat_router: smalltalk light path failed → agent: %s", _lp_exc)
        # //// END Neoffice ////
        # //// Neoffice — the orchestrator gets the reminder instruction, see _ONE_OFF_REMINDER_HINT.
        return {"routed": False, "category": "DIRECT", "ack": None, "task_id": None,
                "agent_hint": _ONE_OFF_REMINDER_HINT if _one_off_reminder else None}

    # RECURRENT — a recurring "do this every X" request. Not a one-shot pole task: create
    # a scheduled task in code (deterministic, user-scoped) via the nora task_router and
    # deliver its ack. Falls back to the agent on any failure (zero regression).
    if category == "recurrent":
        _rec = _route_recurrent(message, chat_user, deliver_extra)
        # //// Neoffice — a DECLINED recurrence is a ONE-SHOT request after all. The nora
        # task_router extracts schedules deterministically; when it finds none ("ok crée
        # un rappel" = a dunning, not a scheduled reminder) the old fallback dropped to
        # the DIRECT agent and LOST the conversation (observed 2026-07-08: after a
        # fast-answer listed the due invoices, the orchestrator replied "je ne sais pas
        # quel rappel…"). Recover IN CODE: prior pole (conversation continuity) first,
        # else a keyword-rule pole; DIRECT only when neither applies (unchanged).
        if _rec.get("routed"):
            note_nora_reply(conversation_id, _rec.get("ack") or "")
            return _rec
        # //// Neoffice — an unsupported cadence is not a one-shot, see _UNSUPPORTED_CADENCE_HINT.
        if _rec.get("reason") == "unsupported_cadence":
            logger.info("nora_chat_router: recurrent cadence not runnable → orchestrator explains")
            return {"routed": False, "category": "DIRECT", "ack": None, "task_id": None,
                    "agent_hint": _UNSUPPORTED_CADENCE_HINT}
        # //// END Neoffice ////
        _prior_pole = (prior or {}).get("pole")
        _kw_pole = next((p for rx, p in _FAST_PATH_RULES if rx.search(message or "")), None)
        _recovered = _prior_pole or _kw_pole
        if not (_recovered and _recovered in POLES):
            return _rec
        logger.info(
            "nora_chat_router: recurrent declined → recovered to pole %s (prior=%s kw=%s)",
            _recovered, _prior_pole, _kw_pole,
        )
        category = _recovered
        # The turn was recorded with pole=None ("recurrent" is not in POLES) — fix it so
        # the NEXT follow-up ("par email", "oui envoie") inherits the recovered pole.
        if conversation_id and conversation_id in _LAST_ROUTE:
            _LAST_ROUTE[conversation_id]["pole"] = _recovered
        # //// END Neoffice ////

    # ── Unify the conversation_id for the ack AND the worker result ──────────────────
    # The ack is delivered via deliver_extra["conversation_id"] (the cid the webhook
    # resolved from the inbound payload — provably the thread the desk polls, since the
    # user sees the ack), while the worker result is delivered via the notify-sub's
    # conversation_id (the raw payload conversation_id). When these two differ — observed
    # 2026-06-03 under the per-user session keying: the result landed in a spurious cid
    # (nora-8f29…) while the ack reached the desk thread — they sit in DIFFERENT desk
    # buffers, and get_replies returns the non-empty primary without falling back to the
    # other → the desk shows the ack but the result is stuck on "Nora consulte…" forever.
    # Force BOTH onto the deliver_extra cid (the ack's, which provably reaches the desk).
    _de_cid = ((deliver_extra or {}).get("conversation_id") or "").strip()
    # //// Neoffice — an UNSUBSTITUTED template placeholder ("{conversation_id}",
    # "{{cid}}"…) must count as ABSENT, not as a real conversation id: the
    # 2026-06-02 incident delivered worker replies "into the void" under the
    # literal placeholder key when a desk template variable wasn't filled.
    # Treat any brace-wrapped value as unset so delivery falls back to the
    # session chat. grep "//// Neoffice".
    if _de_cid.startswith("{") and _de_cid.endswith("}"):
        _de_cid = ""
    _param_cid = (conversation_id or "").strip()
    if _param_cid.startswith("{") and _param_cid.endswith("}"):
        _param_cid = ""
    # //// END Neoffice ////
    _unified_cid = _de_cid or (_param_cid or None)
    logger.info(
        "nora_chat_router cid-unify: session=%s param_cid=%s deliver_extra_cid=%s → unified=%s",
        session_chat_id, conversation_id, _de_cid or None, _unified_cid,
    )

    # //// Neoffice — DETERMINISTIC FAST-PATH for simple data reads. Before spawning a
    # worker (Gemma 12b fumbles tool filters → loops 13-25× → ~25s + an apology), try the
    # nora fast_answer engine: ONE structured-intent LLM call + a Frappe query in CODE
    # (~0.5s, correct). On a hit we deliver it directly — no worker, no loop. French only
    # for now (the engine templates French); other languages fall through to the worker.
    # Any miss/uncertainty → None → normal worker route (zero regression). grep "//// Neoffice".
    if _fa_future is not None:
        try:
            _fa_text = _fa_future.result(timeout=12)
        except Exception as _fa_exc:  # noqa: BLE001 — engine crash = defer to worker
            logger.warning("nora_chat_router: parallel fast_answer failed: %s", _fa_exc)
            _fa_text = None
        if _fa_text:
            _fa_delivered = _post_ack_to_callback(
                _fa_text, {**(deliver_extra or {}), "conversation_id": _unified_cid}
            )
            # //// Neoffice — record the answer in the film: the next turn ("ok crée un
            # rappel") must know WHAT NORA just listed. grep "//// Neoffice".
            note_nora_reply(conversation_id, _fa_text)
            # //// END Neoffice ////
            logger.info(
                "nora_chat_router: FAST-ANSWER chat=%s → %s delivered=%s",
                session_chat_id, category, _fa_delivered,
            )
            return {
                "routed": True, "category": category, "ack": _fa_text,
                "task_id": None, "fast": True, "ack_delivered": _fa_delivered,
            }
    # //// END Neoffice ////

    # Create the task exactly as the kanban_create tool does (assignee + running →
    # the dispatcher spawns the specialist worker). idempotency_key (the webhook
    # delivery id) makes a retried POST reuse the same task instead of double-routing.
    try:
        from hermes_cli import kanban_db, kanban_db_connect, kanban_db_notify

        # kanban_db.connect / add_notify_sub are compat pointers upstream schedules for
        # removal: reach the modules that own them.
        conn = kanban_db_connect.connect(board=board)
        try:
            # Give the WORKER the conversation context too, so a follow-up is executed
            # as a refinement of the prior turn — "Ceux de ce client" after "Combien de
            # devis ouverts ?" must mean "the OPEN DEVIS of client ce client", not "show
            # ce client's profile". The router already kept the right pole; this keeps the
            # right INTENT. Only when the previous turn routed to the same kind of request.
            # //// Neoffice — give the worker the conversation FILM (recent user turns) so a
            # MULTI-STEP request is CONTINUED, not restarted. The worker is session-less, so
            # without this it loses the thread (the client created two turns ago is invisible;
            # the goal stated three turns ago is gone). With the film it re-resolves entities
            # already created (they exist now) and drives to the final goal. Falls back to the
            # single-prior note when there is no longer film. grep "//// Neoffice".
            # Scaffolding fed to the worker LLM is ENGLISH (better model comprehension);
            # the user-facing REPLY stays in the user's language via the directive appended
            # below ([Reply to the user in <lang>]). grep "//// Neoffice".
            _body = message
            _film_prev = (_CONV_HISTORY.get(conversation_id) or [])[:-1]  # prior turns (drop current)
            # //// Neoffice — accepted briefing offer: inject the proposed action so "oui"
            # leads straight to it. Takes priority over the film/prior context. grep "//// Neoffice".
            if _offer:
                _body = (
                    "[The user is replying to NORA's morning-briefing proposal: "
                    f"« {(_offer.get('question_fr') or '').strip()} ». "
                    f"Proposed action: {(_offer.get('action_en') or '').strip()} "
                    "The user's reply is below; if it is affirmative, carry out the proposed "
                    "action NOW and present the result — do not merely acknowledge.]"
                    f"\n\n{message}"
                )
            elif _film_prev:
                _film_lines = "\n".join(f"  {i + 1}. {t}" for i, t in enumerate(_film_prev))
                _body = (
                    "[ONGOING CONVERSATION — the user is carrying out a MULTI-STEP request. "
                    "Previous turns, oldest to newest (User: = the user, NORA: = what the "
                    "assistant already answered — treat NORA lines as facts already shown "
                    "to the user):\n"
                    f"{_film_lines}\n"
                    "Take into account what has already been asked AND created in this thread: "
                    "do NOT redo what is done (entities created in earlier turns ALREADY EXIST — "
                    "look them up), resolve references like « ces factures / le premier » against "
                    "the NORA lines above, and CONTINUE until the request is COMPLETE (not just "
                    "one isolated step). COMPLETE means exactly what the user asked and NOTHING "
                    "more: NEVER create an additional document (invoice, order, payment, delivery "
                    "note…) the user did not explicitly ask for in this thread. "
                    + _BARE_YES_RULE +  # //// Neoffice — see _BARE_YES_RULE
                    " Current message below.]"
                    f"\n\n{message}"
                )
            elif prior and prior.get("pole") and prior.get("msg"):
                _body = (
                    f"[Conversation follow-up — the user first asked: « {prior['msg'][:300]} ». "
                    "The message below refines/continues that request; interpret it in that "
                    "context (a bare name = a customer to filter by).]"
                    f"\n\n{message}"
                )
            # //// Neoffice — the job the user is looking at (docked job panel, 05.09). The
            # desk sends its page context with every message; without it a worker asked
            # about « ce chantier » invented a job number (RT445566, live on osiris).
            _pc = page_context if isinstance(page_context, dict) else {}
            _pc_job = _pc.get("job") if isinstance(_pc.get("job"), dict) else {}
            _pc_project = _page_project(page_context)
            # //// Neoffice — the message itself can name the job, and then it is just
            # //// as certain as the page context. Measured 16.09 on the new `projet`
            # //// pole: « Note une heure de travail sur le chantier PROJ-0087 » arrived
            # //// over WhatsApp (no page context), so no anchor was carried. The worker
            # //// then called frappe_job_status FIVE times WITHOUT its `project`
            # //// argument — the number was in the task title both times, and the model
            # //// simply never copied it. The tool answered "project is required" five
            # //// times, the run ended without calling kanban_complete, and the safety
            # //// net closed the card as `done` with an empty summary: the hour was
            # //// never recorded and nobody was told. Reading the id out of the message
            # //// is code doing what the model was being trusted to do.
            _pc_src = "Page context — the user is on building job"
            if not _pc_project:
                _m_proj = re.search(r"\bPROJ-\d+\b", str(message or ""), re.IGNORECASE)
                if _m_proj:
                    _pc_project = _m_proj.group(0).upper()
                    # Say where it came from: the anchor must never claim a page the
                    # user was not on — a lie in the body is a lie the worker repeats.
                    _pc_src = "The request names the building job"
            # //// END Neoffice ////
            if _pc_project:
                _body = job_page_anchor(category, _pc_src, _pc_project, _pc_job) + "\n\n" + _body
            # //// Neoffice — any other document page, see document_page_anchor.
            elif _doc_anchor := document_page_anchor(page_context):
                _body = _doc_anchor + "\n\n" + _body
            # //// Neoffice — « ce produit » that nothing names, see unnamed_document_hint.
            elif _unnamed := unnamed_document_hint(message, page_context, _film_prev):
                _body = _unnamed + "\n\n" + _body
            # //// Neoffice — « à quatre pièces » is a quantity, see quantity_change_hint.
            if _qty_hint := quantity_change_hint(message):
                _body = _qty_hint + "\n\n" + _body
            # //// END Neoffice ////
            # //// Neoffice — tell the specialist worker which language to answer in (the
            # user's). ALWAYS carry it, FRENCH INCLUDED: the worker SOUL only "leans" FR, and a
            # cold model drifts to English without an explicit per-task directive (observed
            # 2026-06-18: fr user got an English worker summary). Deterministic beats hoping the
            # SOUL holds — the directive is one line at the BACK of the body (cache-safe, the
            # user turn is always last). grep "//// Neoffice".
            _wlang = _norm_lang(language)
            _lang_full = {"fr": "French", "de": "German", "it": "Italian", "en": "English"}.get(
                _wlang, _wlang
            )
            # //// Neoffice — partner guard (2026-08-21, osiris): a worker asked to
            # "order ten" of an item picked a REAL customer (ce client) out of the
            # database — never named in the thread — and created a sales order AND an
            # uninvited invoice in his name. Guessing a business partner fabricates
            # documents in a real person's name; asking costs one turn. Carried on
            # every task body (all readers are kanban workers; DIRECT chat never
            # routes here). grep "//// Neoffice".
            _body = (
                f"{_body}\n\n[Reply to the user in {_lang_full}. Do not reply in any "
                "other language. If a document you are about to create needs a "
                "business partner (customer or supplier) and NO partner is named in "
                "the request or the conversation context, do NOT pick one yourself — "
                "ask the user which partner to use (kanban_block kind=needs_input) "
                "and STOP.]"
            )
            # //// END Neoffice ////
            # //// Neoffice — asked aloud, see voice_answer_hint. At the back of the body with the
            # //// language directive: both say how to reply, not what to do.
            if _voice := voice_answer_hint(page_context):
                _body = f"{_body}\n\n{_voice}"
            # //// END Neoffice ////
            # //// Neoffice — stable worker cwd (re-ported from fork commit c68c362e6 after
            # taking the richer osiris-poc router). The dispatcher runs the worker with
            # cwd=<task workspace>; the default "scratch" workspace is ephemeral (deleted on
            # completion), so the worker's per-pole MCP stdio subprocess fails to start
            # (connected=False / tools=0, proven on the staging soak). A persistent "dir"
            # workspace gives a stable cwd so the MCP server connects. NORA metier workers
            # produce no files → a shared work dir is fine. grep "//// Neoffice".
            import os as _os_ws
            _nora_work = _os_ws.path.join(_os_ws.path.expanduser("~"), ".hermes-nora-work")
            try:
                _os_ws.makedirs(_nora_work, exist_ok=True)
            except OSError:
                _nora_work = None
            _ws_kwargs = (
                {"workspace_kind": "dir", "workspace_path": _nora_work}
                if _nora_work else {}
            )
            # //// END Neoffice ////
            task_id = kanban_db.create_task(
                conn,
                title=(message or "").strip()[:200] or "Demande",
                body=_body,
                assignee=category,
                created_by="nora-chat-router",
                initial_status="running",
                idempotency_key=idempotency_key,
                **_ws_kwargs,  # //// Neoffice — stable worker cwd ////
            )
            _add_notify_sub(
                kanban_db_notify,
                conn,
                task_id=task_id,
                platform="webhook",
                chat_id=session_chat_id,
                thread_id=thread_id or None,
                user_id=user_id or None,
                notifier_profile=notifier_profile,
                conversation_id=_unified_cid,
            )
            # //// Neoffice — wake the dispatcher NOW: without the poke the new
            # task waited for the next periodic tick (0..interval s of dead
            # time; 3 s measured). Best-effort — the tick still guarantees it.
            try:
                from gateway.kanban_watchers import poke_kanban_dispatcher

                poke_kanban_dispatcher()
            except Exception:
                pass
            # //// END Neoffice ////
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        # If we cannot create/subscribe the task, DO NOT silently drop the message —
        # fall back to the agent path (which may still route it). Surface the failure.
        logger.exception("nora_chat_router: task creation failed (%s) → DIRECT fallback", exc)
        return {"routed": False, "category": category, "ack": None, "task_id": None}

    ack = build_ack(category, language)
    # Deliver the immediate ack NOW so the user sees "I'm handing this to <pole>" right
    # away — it also makes the ~10s worker wait feel responsive. Failure to deliver the
    # ack must NEVER fail the routing (the worker result still arrives via the notifier).
    ack_delivered = _post_ack_to_callback(
        ack, {**(deliver_extra or {}), "conversation_id": _unified_cid}
    )
    logger.info(
        "nora_chat_router: routed deterministically chat=%s → %s task=%s ack_delivered=%s",
        session_chat_id, category, task_id, ack_delivered,
    )
    return {
        "routed": True,
        "category": category,
        "ack": ack,
        "task_id": task_id,
        "ack_delivered": ack_delivered,
    }
