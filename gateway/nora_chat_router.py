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

# //// Neoffice — the reply directive a worker reads at the back of its task, built in ONE place for
# //// the two ways a chat task is born: by this router, or by the orchestrator's kanban_create when
# //// the router hands a message on (tools/kanban_tools.py reads it through chat_reply_directive).
# //// The router's tasks carried it; the orchestrator's carried none, so the pole's worker fell back
# //// to its French SOUL: « How many open quotes does <a client> have? » came back in French
# //// (capability bench, 07.10). ALWAYS carried, French included (2026-06-18: a cold model drifts
# //// to English without it). The partner guard rides with it (2026-08-21, osiris: a worker asked to
# //// « order ten » picked a real customer nobody had named and created a sales order and an
# //// invoice in his name; guessing a partner fabricates documents, asking costs one turn).
_WORKER_LANGUAGE_NAMES = {"fr": "French", "de": "German", "it": "Italian", "en": "English"}
REPLY_DIRECTIVE_MARK = "[Reply to the user in "
# The language each chat's person last wrote in, keyed by the session chat id: the id the
# orchestrator's kanban_create reads back from its session (HERMES_SESSION_CHAT_ID).
_CHAT_LANGUAGE: dict = {}


def worker_reply_directive(language: Optional[str]) -> str:
    lang = _norm_lang(language)
    return (
        f"{REPLY_DIRECTIVE_MARK}{_WORKER_LANGUAGE_NAMES.get(lang, lang)}. Do not reply in any "
        "other language. "
        # //// Neoffice — 09.10: in English the amounts came without « CHF », and a German answer
        # //// kept the ERP's French status « Brouillon »; the workers wrote numbers the French way
        # //// (« 30 120,15 CHF ») beside the fast path's Swiss « 30'120.15 ». Said here, not in the
        # //// SOULs, which are at their 20 000-character cap.
        "Write every amount with its currency (CHF), the Swiss way (an apostrophe between the "
        "thousands, a point before the cents: 1'234.50 CHF), and the ERP's own words (a status such "
        "as « Brouillon ») in that language too. If a document you are about to create needs a "
        "business partner (customer or supplier) and NO partner is named in "
        "the request or the conversation context, do NOT pick one yourself — "
        "ask the user which partner to use (kanban_block kind=needs_input) "
        "and STOP.]"
    )


def remember_chat_language(session_chat_id: Optional[str], language: Optional[str]) -> None:
    if not session_chat_id:
        return
    if len(_CHAT_LANGUAGE) > _LAST_ROUTE_MAX:
        _CHAT_LANGUAGE.clear()
    _CHAT_LANGUAGE[session_chat_id] = _norm_lang(language)


def chat_reply_directive(session_chat_id: Optional[str]) -> Optional[str]:
    """The directive for a task created from this chat; None when no message of it came through here."""
    lang = _CHAT_LANGUAGE.get(session_chat_id or "")
    return worker_reply_directive(lang) if lang else None
# //// END Neoffice ////

# //// Neoffice — a chat without a conversation_id has a thread all the same (09.10). The desk sends one
# //// per thread; a WhatsApp payload has none, and every piece of the router's context (the prior pole,
# //// the film, a pending question) keys on it: on WhatsApp « oui » or « et le mois dernier ? » was routed
# //// as a first message, and the worker of a follow-up never saw what came before. The chat (the
# //// number, session_key: phone) is the thread; it starts afresh after _CHAT_THREAD_IDLE seconds
# //// without a message from either side, as a new desk thread would.
import time as _time_thread

_CHAT_THREAD_IDLE = 1800
_CHAT_THREADS: dict = {}  # session_chat_id -> [thread key, last activity]
_thread_now = _time_thread.time


def chat_thread(session_chat_id: Optional[str]) -> Optional[str]:
    """The thread key of a chat that sends no conversation_id, renewed after _CHAT_THREAD_IDLE of silence."""
    if not session_chat_id:
        return None
    now = _thread_now()
    current = _CHAT_THREADS.get(session_chat_id)
    if current is None or now - current[1] > _CHAT_THREAD_IDLE:
        if len(_CHAT_THREADS) > _LAST_ROUTE_MAX:
            _CHAT_THREADS.clear()
        current = [f"{session_chat_id}#{int(now)}", now]
        _CHAT_THREADS[session_chat_id] = current
    current[1] = now
    return current[0]


def _touch_chat_thread(key: str) -> None:
    """NORA's reply keeps its chat thread alive: the person answers it, not their own last message."""
    current = _CHAT_THREADS.get(key.rpartition("#")[0])
    if current and current[0] == key:
        current[1] = _thread_now()
# //// END Neoffice ////

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


# //// Neoffice — a yes in a thread where NORA has said nothing yet confirms nothing (#1065).
# //// The agent's session is kept per PERSON, the film per thread: a fresh Quick Chat thread
# //// opened on « Oui, vas-y. », and the orchestrator found in its session a proposal from
# //// another thread of the morning and handed it to a pole. Fresh is known only when nora
# //// says so (`nora_spoke`, read from its chat log, which outlives a gateway restart) and this
# //// gateway's film agrees. Unknown (WhatsApp, an older nora): routed as before.
_NOTHING_PROPOSED = {
    "fr": "Je n'ai rien en attente de votre accord dans cette conversation, je n'ai donc rien fait. "
          "Que puis-je faire pour vous ?",
    "de": "In diesem Gespräch wartet nichts auf Ihre Zustimmung, ich habe also nichts ausgeführt. "
          "Was kann ich für Sie tun?",
    "it": "In questa conversazione non c'è nulla in attesa del suo consenso, quindi non ho fatto nulla. "
          "Cosa posso fare per lei?",
    "en": "Nothing in this conversation is waiting for your approval, so I haven't done anything. "
          "What can I do for you?",
}
# A message that opens on a yes (« Oui, fais-le », « ok go »): in a fresh thread it points at nothing.
_YES_HEAD_RE = re.compile(
    r"^\s*(?:oui|ouais|ok(?:ay)?|d['\u2019]?accord|bien\s+s[uû]r|vas[- ]?y|allez[- ]?y|volontiers|parfait"
    r"|go|yes|yeah|yep|sure|ja|jawohl|gerne|klar|s[iì]|certo|va\s+bene)\b",
    re.IGNORECASE,
)
_FRESH_THREAD_HINT = (
    "[This conversation is NEW: NORA has proposed nothing in it yet. A yes, an « ok » or a « do it » "
    "here confirms nothing: never carry out a proposal, an action or a task from another "
    "conversation of this person, nor one recalled from memory. Ask what they want.]"
)


def _thread_is_fresh(conversation_id: Optional[str], nora_spoke: Optional[bool]) -> bool:
    """True only when it is KNOWN that NORA has said nothing yet in this thread."""
    if not conversation_id or nora_spoke is not False:
        return False
    return not any(line.startswith("NORA: ") for line in _CONV_HISTORY.get(conversation_id) or [])


def _nothing_proposed_reply(message: str, language: Optional[str]) -> Optional[str]:
    """The answer to a bare yes (« oui », « Oui, vas-y. », « ok ») in a fresh thread, else None.
    « Merci » alone is small talk, not a yes: it keeps its canned reply."""
    msg = (message or "").strip()
    if not (_NOTE_BARE_ACK_RE.match(msg) and _YES_HEAD_RE.match(msg)):
        return None
    return _NOTHING_PROPOSED.get(_norm_lang(language)) or _NOTHING_PROPOSED["fr"]
# //// END Neoffice ////


# //// Neoffice — a bare yes is titled by what it confirms (08.10). A worker's result reads « ✅ <pole> —
# //// <title> », and the title was the message: « ✅ Ventes — Oui, vas-y. » named nothing (Quick Chat, a
# //// price change confirmed). A bare yes takes the title of the request it answers, and a second yes keeps it.
def _task_title(message: Optional[str], prior: Optional[dict]) -> str:
    msg = (message or "").strip()
    confirmed = (prior or {}).get("title") or (prior or {}).get("msg")
    if confirmed and _NOTE_BARE_ACK_RE.match(msg) and _YES_HEAD_RE.match(msg):
        return confirmed
    return msg[:200] or "Demande"
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
    _touch_chat_thread(conversation_id)  # //// Neoffice — a chat thread, see chat_thread (09.10)
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
    # //// Neoffice — recruitment (07.10, maintenance#1294): the rh pole lists and reads the
    # //// applications hrms receives on the job page. « offre » alone is still a quotation.
    "  Et le RECRUTEMENT : offres d'emploi publiées sur le site, candidatures reçues, "
    "candidats, leur dossier et les questions d'entretien (« qui a postulé ? »).\n"
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
    "'direct' pour une vraie demande métier, choisis le pôle.\n"
    # //// Neoffice — an English business question came back 'direct' (capability bench, 07.10:
    # //// « How many open quotes does <a client> have … ? »): the rules above are written in French.
    "La langue du message ne change rien : une demande en anglais, en allemand ou en italien "
    "suit exactement les mêmes règles (« How many open quotes … ? », « Wie viele offene "
    "Rechnungen … ? » vont à leur pôle, jamais à 'direct').\n\n"
    # //// END Neoffice ////
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
    # //// Neoffice — and the candidates of a recruitment (07.10).
    "employés internes et candidats à un poste uniquement)."
    # //// END Neoffice ////
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

# //// Neoffice — a client's or supplier's contact change, as a request (« change l'adresse de … »)
# //// and as a fact (« … a déménagé »): named, so the employee check of route_chat_message (#843)
# //// reads the same words as the rules below. See the rules for what each half excludes.
_CONTACT_CHANGE_RE = re.compile(
    r"^(?!.*\b(?:employ[ée]e?|collaborat\w*|salari[ée]e?|mitarbeiter\w*|dipendente)\b)"
    r".*\b(?:change[rz]?|modifie[rz]?|met[sz]?\s+[àa]\s+jour|corrige[rz]?|remplace[rz]?|update|"
    r"[äa]ndere|aggiorna)\b.{0,40}?\b(?:e-?mail|adresse\s+e-?mail|courriel|t[ée]l[ée]phone|"
    r"num[ée]ro\s+de\s+(?:t[ée]l[ée]phone|portable|mobile)|mobile|natel|adresse|email|phone|address|"
    r"telefon|indirizzo)\b",
    re.IGNORECASE,
)
_CONTACT_FACT_RE = re.compile(
    r"^(?!.*\b(?:employ[ée]e?|collaborat\w*|salari[ée]e?|mitarbeiter\w*|dipendente)\b)"
    r"(?!.*(?:\benvoi|\benvoy|\b[ée]cri[rstvez]|\br[ée]dig|\btransmet|\badresse-(?:lui|leur|moi)\b))"
    r".*(?:\bnouve(?:lle|au|l)s?\s+(?:adresse(?:\s+e-?mail)?|e-?mail|courriel|"
    r"num[ée]ro(?:\s+de\s+(?:t[ée]l[ée]phone|portable|mobile))?|t[ée]l[ée]phone|mobile|natel)\b"
    r"|\ba\s+chang[ée]\s+d['’]\s*(?:adresse|e-?mail|num[ée]ro|t[ée]l[ée]phone)"
    r"|\ba\s+d[ée]m[ée]nag[ée]\b"
    r"|\bchangement\s+d['’]\s*(?:adresse|e-?mail|num[ée]ro))",
    re.IGNORECASE,
)
# //// END Neoffice ////

# //// Neoffice — VALIDATING a document goes to a pole that can validate it (07.10). In a
# //// colleague's demonstration the sales pole drafted an invoice and offered « Voulez-vous que
# //// je la valide et que je lui envoie un email ? »; the yes named the mail, so the e-mail rule
# //// below sent it to support, which holds no tool to validate a document, and support answered
# //// that it could not. The document poles (ventes, compta, projet) hold frappe_document_submit
# //// and ventes holds send_email too. Purchase-side and payment documents go to compta.
_VALIDATE_VERB_RE = re.compile(
    r"\b(?:valide[rsz]?|validez|validons|soumet[st]?|soumettre|soumettez|comptabilise[rz]?)\b",
    re.IGNORECASE,
)
_VALIDATE_PURCHASE_RE = re.compile(
    r"(?=[\s\S]*\b(?:valide[rsz]?|validez|validons|soumet[st]?|soumettre|soumettez|comptabilise[rz]?)\b)"
    r"(?=[\s\S]*\b(?:fournisseurs?|achats?|paiements?|encaissements?|r[èe]glements?|ACC-PAY-[\w-]+|"
    r"ACC-PINV-[\w-]+)\b)",
    re.IGNORECASE,
)
_VALIDATE_DOCUMENT_RE = re.compile(
    r"(?=[\s\S]*\b(?:valide[rsz]?|validez|validons|soumet[st]?|soumettre|soumettez|comptabilise[rz]?)\b)"
    r"(?=[\s\S]*\b(?:factur\w*|devis|offres?|commandes?|bons?\s+de\s+(?:livraison|commande)|avoirs?|"
    r"FA-\d[\w-]*|DEVIS-\d[\w-]*|BC-\d[\w-]*|BL-\d[\w-]*)\b)",
    re.IGNORECASE,
)
_DOCUMENT_POLES = frozenset({"ventes", "compta", "projet"})


def _validation_stays(msg: str, prior_pole: Optional[str], rule_pole: Optional[str]) -> bool:
    """A validation the e-mail rule would send to support stays with the document pole that offered it."""
    return rule_pole == "support" and prior_pole in _DOCUMENT_POLES and bool(_VALIDATE_VERB_RE.search(msg or ""))
# //// END Neoffice ////

# //// Neoffice — « oui, relance-la » after the rh pole offered to read an application again stays
# //// with rh (07.10, maintenance#1294). The dunning rule's \brelanc would send that yes to compta,
# //// which holds no application. Only when nothing names a collection: « relance les clients »,
# //// « relance la facture de Martin » still reach compta.
_RELANCE_RE = re.compile(r"\brelanc\w*", re.IGNORECASE)
_COLLECTION_OBJECT_RE = re.compile(
    r"\b(?:factur\w*|paiements?|impay\w*|d[ée]biteurs?|clients?|montants?|DUNN-\w+|FA-\d[\w-]*)\b"
    r"|\brappels?\s+de\s+(?:paiement|facture)\b",
    re.IGNORECASE,
)


def _rh_relance_stays(msg: str, prior_pole: Optional[str], rule_pole: Optional[str]) -> bool:
    """A « relance » answering the rh pole's own offer stays with rh, unless it names a collection."""
    msg = msg or ""
    return (
        prior_pole == "rh"
        and rule_pole == "compta"
        and bool(_RELANCE_RE.search(msg))
        and not _COLLECTION_OBJECT_RE.search(msg)
    )
# //// END Neoffice ////

# //// Neoffice — the bare « rappel(s) » rule of the table below (#1268): the noun, never
# //// the verb, unless the message is about a calendar or a phone callback.
_BARE_REMINDER_RE = re.compile(
    r"^(?![\s\S]*\b(?:agenda|calendrier|rendez[- ]vous|rdv|r[ée]unions?|[ée]v[ée]nements?|"
    r"anniversaires?|t[ée]l[ée]phon\w*|demandes?\s+de\s+rappel)\b)"
    r"[\s\S]*(?:\brappels?\b|\bmahnung(?:en)?\b|\bzahlungserinnerung(?:en)?\b|\bsollecit[io]\b|"
    r"\bdunnings?\b|\bpayment\s+reminders?\b)",
    re.IGNORECASE,
)
# //// END Neoffice ////

# //// Neoffice — recruitment is rh's (07.10, maintenance#1294). hrms gains a job page whose
# //// applications the model reads (summary, scores, what is missing, interview questions), and
# //// the rh pole the tools to list and read them. Without a rule, « Relance la lecture de cette
# //// candidature » reached compta (the dunning rule's \brelanc), « crée une offre d'emploi pour un
# //// chef de chantier » and « fixe un rendez-vous avec la candidate » reached projet (the job
# //// rules that run before the table), and « l'offre de chef de projet » reached projet too (the
# //// quotation-of-a-job rule). So it is tested before all of them, below the chart rule only.
# //// « offre » alone is a QUOTATION in Swiss French: it counts here only as a job word (offre
# //// d'emploi, de stage), next to « critères », or as an advert published on the website, never a
# //// shop's « offre spéciale / du mois », and never « une offre pour la refonte du site ».
# //// Left out: a tender (« dossier de candidature » for a public contract is a sale), and a
# //// message that asks to SEND a mail, which keeps reaching the e-mail rule: support holds the
# //// mail tools, rh holds none.
_PROMO_OFFER = (
    r"(?![^.!?]{0,25}?\b(?:sp[ée]ciale?s?|promo\w*|du\s+(?:jour|mois|moment)|"
    r"de\s+(?:no[eë]l|saison|bienvenue|lancement)|exclusive?s?|limit[ée]e?s?)\b)"
)
# //// Neoffice — the account an expense goes to is compta's, whatever the expense paid for (09.10).
# //// « Dans quel compte imputer la commission d'un cabinet pour recruter un employé ? » reached rh: the
# //// recruitment rule sits above the allocation rule in the table, and rh holds no chart of accounts
# //// (gate llm/27, three runs out of three). Shared by the allocation rule below and, as an exclusion,
# //// by the recruitment rule: recruiting stays rh's, the account of what it cost is compta's.
_ACCOUNT_ALLOCATION = (
    r"\bplan\s+comptable\b|\bimputation\s+comptable\b"
    r"|\b(?:dans|sur)\s+quel\s+compte\b[^.!?]{0,60}?\b(?:imput\w*|pass\w*|comptabilis\w*|enregistr\w*|"
    r"class\w*|mettre|mets|saisi\w*)"
    r"|(?=[\s\S]*\b(?:factur\w*|tickets?\b|d[ée]pens\w*|achats?\b|frais\b|honoraires?\b|commissions?\b))"
    r"(?=[\s\S]*\b(?:imput\w*|comptes?\s+(?:de\s+)?(?:charge|comptable)))"
)
_ACCOUNT_ALLOCATION_RE = re.compile(_ACCOUNT_ALLOCATION, re.IGNORECASE)
# //// END Neoffice ////
_RECRUITMENT_RE = re.compile(
    r"^(?![\s\S]*?(?:" + _ACCOUNT_ALLOCATION + r"))"  # //// Neoffice — see _ACCOUNT_ALLOCATION (09.10)
    r"(?![\s\S]*\b(?:appels?\s+d['’]\s*offres?|soumissions?|march[ée]s?\s+publics?|adjudications?|"
    r"ausschreibung(?:en)?|bandi|bando|appalt\w*|tenders?)\b)"
    r"(?!(?=[\s\S]*\b(?:e-?mails?|mails?|courriels?)\b)"
    r"(?=[\s\S]*\b(?:envoi\w*|envoy\w*|[ée]cri[rstvez]\w*|r[ée]dig\w*|transmet\w*|send\w*|writ\w*|"
    r"draft\w*|schreib\w*|schick\w*|scriv\w*|invi[ao]\w*)\b))"
    r"[\s\S]*?(?:"
    # French
    r"\bcandidat(?:e|es|s)?\b|\bcandidatures?\b|\brecrut\w*|\bpostul\w*|\bembauch\w*"
    r"|\boffres?\s+(?:d['’]\s*emplois?|de\s+(?:stage|poste))\b|\bannonces?\s+d['’]\s*emplois?\b"
    r"|\bpostes?\s+(?:[àa]\s+pourvoir|vacants?|ouverts?)\b|\blettres?\s+de\s+motivation\b"
    r"|\b(?:le|son|sa|ses|leur|leurs|ce|du|des|les|mon|ton|un|une)\s+(?:cvs?|curriculum)\b"
    r"|\bentretiens?\s+d['’]\s*embauche\b|\bquestions?\b[^.!?]{0,60}?\b(?:en\s+|d['’]\s*)entretiens?\b"
    r"|\bcrit[èe]res?\b[^.!?]{0,60}?\boffres?\b"
    r"|\b(?:publi\w*|d[ée]publi\w*|(?:mets|mettez|mettre)\s+en\s+ligne)\b[^.!?]{0,60}?\boffres?\b" + _PROMO_OFFER
    + r"|\boffres?\b" + _PROMO_OFFER + r"[^.!?]{0,60}?\b(?:publi[ée]e?s?|en\s+ligne)\b"
    r"|\b(?:retir\w*|enl[èe]v\w*)\b[^.!?]{0,60}?\boffres?\b" + _PROMO_OFFER
    + r"[^.!?]{0,60}?\b(?:du|de\s+(?:notre|mon|ce)|sur\s+(?:le|notre))\s+site\b"
    # German
    r"|\bbewerb\w*|\bstellen(?:angebot|ausschreibung|anzeige|inserat)\w*|\boffene[nr]?\s+stellen?\b"
    r"|\bvorstellungsgespr[äa]ch\w*|\blebenslauf\w*|\brekrut\w*|\bkandidat(?:in|innen|en)?\b"
    # Italian
    # //// Neoffice — « offerta » too (08.10): `offerte?` read the plural only, so « un'offerta di
    # //// lavoro » matched nothing here and reached the sales rule for an Italian quotation.
    r"|\bcandidat[aoi]\b|\boffert[ae]\s+di\s+lavoro\b|\bannunci?o?\s+di\s+lavoro\b"
    r"|\bposizion[ei]\s+apert[ae]\b|\bcolloqui?o?\s+di\s+lavoro\b|\breclut\w*"
    # English
    r"|\bjob\s+(?:applications?|openings?|postings?|offers?|ads?|adverts?|interviews?)\b|\bapplicants?\b"
    r"|\bvacanc(?:y|ies)\b|\brecruit\w*|\bhiring\b|\binterview\s+questions?\b|\bcover\s+letters?\b"
    r")",
    re.IGNORECASE,
)
# //// END Neoffice ////


# //// Neoffice — a chart asked for in the fleet's three other languages (08.10). The French rule
# //// at the top of the table knows « graphique » and « tableau de bord », nothing else; these words
# //// went to the classifier, which picks analyse. Needed since the sales-document rule below: « show
# //// the open quotes as a chart » would otherwise reach ventes. « chart of accounts » is the ledger.
_FOREIGN_CHART_RE = re.compile(
    r"\bcharts?\b(?!\s+of\s+accounts?)|\bgraphs?\b|\bdashboards?\b|diagramm\w*"
    r"|\bgrafik(?:en)?\b|\bgrafic[oi]\b",
    re.IGNORECASE,
)

# //// Neoffice — sales documents named in the fleet's three other languages (08.10), the
# //// counterpart of the French « devis / commande client » rule. « How many open quotes does <a
# //// client> have, and what is their total amount? » matched no rule, the classifier answered
# //// 'direct', and the orchestrator handed it on without the person's language: the answer came
# //// back in French, in 15.7 s where the French question takes 7 (capability bench, 07.10).
# //// Nouns only, like the French rule; « orders » alone stays with the classifier (a purchase
# //// order is not a customer's). Two cases are left to the classifier, as before:
# ////   · a job named in the message (« the quote for the construction site »): projet's;
# ////   · a short question about what NORA can do (« can you make quotes? »), the case
# ////     _CAPABILITY_RE answers in French — unless it asks to be told or shown something.
_FOREIGN_SALES_DOCUMENT_RE = re.compile(
    r"^(?![\s\S]*\b(?:construction\s+sites?|job\s+sites?|baustell\w*|cantier[ei]|chantiers?|PROJ-\d+)\b)"
    r"(?!\s*(?:can|could)\s+you\b(?![^?]*\b(?:tell|show|give|list|find|send)\s+(?:me|us)\b)[^?0-9@]{0,90}\?\s*$)"
    r"(?!\s*(?:kannst\s+du|k[öo]nnen\s+sie|k[öo]nntest\s+du)\b(?![^?]*\b(?:mir|uns)\b)[^?0-9@]{0,90}\?\s*$)"
    r"(?!\s*(?:puoi|pu[òo]|potresti|potrebbe)\b(?![^?]*\b(?:dirmi|mostrarmi|darmi|elencarmi|mi)\b)"
    r"[^?0-9@]{0,90}\?\s*$)"
    r"[\s\S]*?(?:\bquot(?:e|es|ation|ations)\b|\b(?:sales|customer)\s+orders?\b"
    r"|\bangebot(?:e|en|s)?\b|\bofferten?\b|\bkundenauftr[äa]g\w*"
    r"|\bpreventiv[oi]\b|\bofferta\b(?!\s+di\s+lavoro)|\bordin[ei]\s+(?:de[il]\s+|di\s+)?client[ei]\b)",
    re.IGNORECASE,
)
# //// END Neoffice ////


_FAST_PATH_RULES = (
    (re.compile(r"(graphique|en graphique|visuel|visualise|dataviz|tableau de bord|histogramme|camembert|courbe|diagramme)", re.IGNORECASE), "analyse"),
    # //// Neoffice — the same in English, German and Italian, see _FOREIGN_CHART_RE (08.10).
    (_FOREIGN_CHART_RE, "analyse"),
    # //// END Neoffice ////
    # //// Neoffice — recruitment, see _RECRUITMENT_RE (07.10). Here so the go-ahead guard sees it too.
    (_RECRUITMENT_RE, "rh"),
    # //// END Neoffice ////
    # //// Neoffice — CHANGING a customer's or supplier's e-mail, phone or address is ventes'
    # //// (frappe_party_contact_update). « Change l'adresse e-mail de <un client> »
    # //// reached support, which has no such tool: tool_search five times (bench, 24.09).
    # //// Not for an employee (rh keeps personnel records).
    (_CONTACT_CHANGE_RE, "ventes"),
    # //// END Neoffice ////
    # //// Neoffice — the same change told as a FACT, with no « change » verb (25.09):
    # //// « Martin SA a une nouvelle adresse e-mail : … », « … a changé d'adresse e-mail »,
    # //// « nouveau numéro de téléphone pour … », « … a déménagé : rue du Lac 12 ». The first
    # //// two reached support through the e-mail rule below; the others fell to the
    # //// classifier. Never when the message asks to SEND something (« écris un courriel à
    # //// la nouvelle adresse de Martin SA » stays support), never for an employee (rh keeps
    # //// personnel records), and « a déménagé » only in the third person: « nous avons
    # //// déménagé » is the company itself, not a client.
    (_CONTACT_FACT_RE, "ventes"),
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
    # //// « un rappel pour le devis » too (#1268, 07.10): the bare-reminder rule below
    # //// sends a lone « rappel » to compta, and a quotation is not an invoice.
    (
        re.compile(
            r"(?=.*\b(?:relanc\w*|rappels?)\b)(?=.*\b(?:devis|offres?|quotations?|DEVIS-\d+)\b)",
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
    # //// Neoffice — a BARE « rappel(s) » is a payment reminder too (#1268, 07.10). Asked
    # //// aloud on the dev instance right after a sales question, « Ok. Est-ce qu'il y a des
    # //// rappels à faire ? » reached the sales pole: the rule above wants « rappel DE
    # //// paiement / DE facture », so a lone « rappels » matched nothing. In an ERP a
    # //// « rappel » is first the reminder of an unpaid invoice (Jérémy: « quand on parle de
    # //// rappel, on parle de rappel de facture, avant tout »), and so are « Mahnung » and
    # //// « sollecito ». The noun only: « rappelle-moi … » / « rappeler » (remind me, call
    # //// back) are verbs and never match. Left to the classifier: a calendar reminder
    # //// (agenda, réunion, rendez-vous, événement) and a phone callback (demande de
    # //// rappel, rappel téléphonique). A one-off « ajoute un rappel demain à 10 h » is
    # //// settled before this table (_is_one_off_reminder) and goes to NORA.
    (_BARE_REMINDER_RE, "compta"),
    # //// END Neoffice ////
    # //// Neoffice — validating a document, see _VALIDATE_DOCUMENT_RE (07.10). Above the e-mail
    # //// rule, so « valide la facture et envoie-la par mail » reaches a pole that can validate it;
    # //// below the reminder rules, so « valide le rappel » stays compta's (frappe_dunning_send).
    (_VALIDATE_PURCHASE_RE, "compta"),
    (_VALIDATE_DOCUMENT_RE, "ventes"),
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
    (_ACCOUNT_ALLOCATION_RE, "compta"),  # //// Neoffice — widened on 09.10, see _ACCOUNT_ALLOCATION
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
    # //// Neoffice — the same documents in English, German and Italian, see _FOREIGN_SALES_DOCUMENT_RE.
    (_FOREIGN_SALES_DOCUMENT_RE, "ventes"),
    # //// END Neoffice ////
    # //// Neoffice — expense claims, salary certificates and source tax go to rh
    # (2026-09-23): the pole now holds the tools to file, list and decide an expense
    # claim and the payroll recaps. « note de frais » used to fall to the classifier,
    # which sent it to compta; compta keeps the same expense tools as a fallback.
    (
        re.compile(
            # //// Neoffice — « paie » only as the payroll NOUN, after a determiner (« la paie de
            # //// septembre », « de paie »), « paye » too. The bare word also caught the verb:
            # //// « Qui paie en retard ? » reached rh, which holds no receivables tool, and its
            # //// worker listed invoices for 2 min before the loop guard (27.09, development instance).
            r"(cong[ée]s?\b|fiche de paie|bulletin de salaire"
            r"|\b(?:la|ma|sa|ta|notre|votre|leur|de|les|mes|ses|des)\s+pa(?:ie|ye)s?\b|absences? (du|des)"
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
    # //// Neoffice — recruitment before the three job rules (07.10, see _RECRUITMENT_RE): to them
    # //// « fixe un rendez-vous avec la candidate » is a visit and « crée une offre d'emploi pour un
    # //// chef de chantier » a job order. The table still decides, so a chart goes to analyse.
    if _RECRUITMENT_RE.search(msg):
        return next(pole for rx, pole in _FAST_PATH_RULES if rx.search(msg))
    # //// END Neoffice ////
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


# //// Neoffice — a reminder asked WITHOUT its moment (09.10). « Rappelle-moi d'appeler le fournisseur
# //// de carrelage » went to the orchestrator, which set it at a time it made up (today 09:00) after
# //// 35 s, and « Demain à 10h » that followed set a SECOND one. The moment is asked in code, the
# //// request kept 10 minutes, and the answer completes it for nora's route_reminder. Only « of
# //// doing » forms: « rappelle-moi le nom du client » asks for information, not for a reminder.
_REMINDER_TO_DO_RE = re.compile(
    r"\b(?:rappelle[rz]?[- ]moi\s+(?:de|d['’]|que)|fais[- ]moi\s+penser\s+(?:[àa]|de|d['’]|que)|"
    r"(?:mets|mettre|mettez|cr[ée]e[rz]?|ajoute[rz]?)[- ](?:moi\s+)?un\s+rappel\s+(?:pour|de|d['’])|"
    r"remind\s+me\s+(?:to|that|about)|erinnere?\s+mich\s+(?:an|daran|dass)|ricordami\s+(?:di|che))\b",
    re.IGNORECASE,
)
_REMINDER_WHEN = {
    "fr": "Pour quand voulez-vous ce rappel ? Par exemple « demain à 10 h » ou « vendredi à 8 h ».",
    "de": "Wann soll ich Sie daran erinnern? Zum Beispiel « morgen um 10 Uhr ».",
    "it": "Quando vuole che glielo ricordi? Per esempio « domani alle 10 ».",
    "en": "When should I remind you? For example « tomorrow at 10am ».",
}
_PENDING_REMINDER: dict = {}
_PENDING_REMINDER_TTL = 600  # seconds the question stays open


def _needs_reminder_moment(msg: str) -> bool:
    """« Rappelle-moi d'appeler X »: a reminder of something to do, with no moment and no habit."""
    msg = msg or ""
    return bool(
        _REMINDER_TO_DO_RE.search(msg)
        and not _REMINDER_MOMENT_RE.search(msg)
        and not _PAYMENT_REMINDER_RE.search(msg)
        and not _RECUR_RE.search(msg)
    )


def _take_pending_reminder(conversation_id: Optional[str], message: str) -> Optional[str]:
    """The reminder NORA asked the moment of, completed by *message* when it gives one; else None.
    The question is consumed by a refusal or by anything else; a bare yes leaves it open."""
    if not conversation_id:
        return None
    pending = _PENDING_REMINDER.pop(conversation_id, None)
    if not pending or _time_note.time() - pending[0] > _PENDING_REMINDER_TTL:
        return None
    msg = (message or "").strip()
    if _NOTE_BARE_ACK_RE.match(msg):
        _PENDING_REMINDER[conversation_id] = pending
        return None
    if not msg or _NOTE_CANCEL_RE.match(msg) or not _REMINDER_MOMENT_RE.search(msg) or _RECUR_RE.search(msg):
        return None
    return f"{pending[1].rstrip(' .!')} {msg}"
# //// END Neoffice ////


# //// Neoffice — the person's SCHEDULED tasks are the orchestrator's (09.10): on the chat she holds
# //// nora_list_tasks, nora_pause_task and nora_delete_task (mcp-tasks), and no pole and no code route
# //// lists, pauses or deletes one. « Mets en pause la tâche des devis ouverts » went to ventes (the
# //// word « devis »), which has no such tool: it wrote the internal tool's name to the person and sent
# //// them to their administrator, and « Supprime-la », « Oui » followed it there (Quick Chat, 09.10).
_TASK_MANAGE_VERB_RE = re.compile(
    r"\b(?:mets?|mettez|mettre)[- ](?:(?:la|le|les)\s+)?en\s+pause\b|\bpause\b|\bsuspend\w*|"
    r"\barr[eê]t\w*|\bstopp?\w*|\bd[ée]sactiv\w*|\br[ée]activ\w*|\bsupprim\w*|\beffac\w*|\bretir\w*|"
    r"\bdelete\b|\bremove\b|\bdisable\b|\bresume\b|\bl[öo]sch\w*|\bpausier\w*|\bdeaktivier\w*|"
    r"\belimin\w*|\bcancella\w*|\bsospend\w*|\bdisattiv\w*|\briattiv\w*",
    re.IGNORECASE,
)
_SCHEDULED_TASK_RE = re.compile(
    r"\bt[aâ]ches?\s+(?:programm|planifi|automati|r[ée]curren)\w*|\brel[eè]ves?\b|"
    r"\brappels?\s+r[ée]currents?\b|\b(?:scheduled|recurring)\s+(?:tasks?|jobs?)\b|"
    r"\b(?:geplante|wiederkehrende)\w*\s+aufgabe\w*|\battivit[aà]\s+(?:programmat|pianificat|ricorrent)\w*",
    re.IGNORECASE,
)
_TASK_REFERENCE_RE = re.compile(r"\bt[aâ]ches?\b|\btasks?\b|\baufgabe\w*|\battivit[aà]\b", re.IGNORECASE)
_PAUSE_RE = re.compile(r"\bpause\b|\bsuspend\w*|\bpausier\w*|\bsospend\w*", re.IGNORECASE)


def _manages_scheduled_tasks(msg: str, prior: Optional[dict]) -> bool:
    """« Mets en pause la tâche des devis ouverts », « supprime ma relève du matin », or « supprime-la »,
    « oui » right after a turn about the scheduled tasks: the orchestrator's, never a pole's."""
    msg = (msg or "").strip()
    if not msg:
        return False
    verb = bool(_TASK_MANAGE_VERB_RE.search(msg))
    if verb and _SCHEDULED_TASK_RE.search(msg):
        return True
    # A job's task is closed or reassigned, never paused: a pause names a scheduled one.
    if _PAUSE_RE.search(msg) and _TASK_REFERENCE_RE.search(msg):
        return True
    if prior and prior.get("tasks"):
        # Only a SHORT follow-up (« supprime-la », « oui », « non, garde-la », « et la tâche du
        # vendredi ? »): « ok crée un rappel pour les impayés » starts with a yes and is a new request.
        words = len(re.findall(r"\w+", msg))
        reference = bool(_TASK_REFERENCE_RE.search(msg))
        if verb and (words <= 5 or reference):
            return True
        if (_YES_HEAD_RE.match(msg) or _NEGATE_RE.search(msg)) and words <= 4:
            return True
        return reference and words <= 8
    return False


_SCHEDULED_TASKS_HINT = (
    "[Route: this turn is about the person's SCHEDULED tasks (the recurring jobs NORA runs for them), "
    "not about a business document. Do NOT call kanban_create. List them with nora_list_tasks; to stop "
    "one, nora_pause_task; to remove one, ask them to confirm, then nora_delete_task. Pass user and "
    "conversation_id exactly as the message gives them. A task's time or cadence cannot be changed here: "
    "offer to delete it so that they ask for it again as they want it.]"
)
# //// END Neoffice ////


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


# //// Neoffice — a NOTE for the person asking is written in code by nora (notes.route_note,
# //// #1040). « Crée-moi une note : … pour le chantier … » reached the projet pole (the word
# //// « chantier »), whose only note tool writes a project's diary and needs a project the
# //// account could not read; « Prends note : … » reached the orchestrator, which answered
# //// « C'est noté » with nothing written. No pole held a tool for a free note. The words of a
# //// note request are unambiguous, so the code routes it, like a one-off reminder. They are
# //// nora's own patterns (nora/api/v2/notes.py): tests/gateway/note_request_vectors.json is
# //// shared with nora byte for byte, so the two sides read the same requests.
_NOT_A_NOTE_RE = re.compile(
	r"\bnotes?\s+(?:de\s+frais|de\s+cr[ée]dit|de\s+d[ée]bit|de\s+livraison|d['\u2019]honoraires?)"
	r"|\b(?:credit|debit|delivery|expense)\s+notes?\b|\bexpense\s+claims?\b"
	r"|\bgutschrift\w*|\blieferschein\w*|\bspesen\w*"
	r"|\bnot[ae]\s+(?:di\s+)?(?:credito|debito|spese)\b",
	re.IGNORECASE,
)

# What may open the request before its command: NORA's name, a greeting, politeness, and
# « can you … » in each language (« Nora, peux-tu me créer une note : … »).
_NOTE_PREFIX = (
	r"^\s*(?:(?:nora|bonjour|salut|hello|hallo|hi|ciao)\b[\s,!:.]*)?"
	r"(?:(?:s['\u2019]il\s+(?:te|vous)\s+pla[iî]t|stp|svp|bitte|per\s+favore|please)\b[\s,!:.]*)?"
	r"(?P<ask>(?:est[- ]ce\s+que\s+)?(?:tu\s+(?:peux|pourrais)|peux[- ]tu|pourrais[- ]tu|vous\s+pouvez|"
	r"pouvez[- ]vous|pourriez[- ]vous|kannst\s+du|k[öo]nnen\s+sie|k[öo]nntest\s+du|puoi|potresti|"
	r"pu[òo]|can\s+you|could\s+you|would\s+you|will\s+you)\s+(?:me\s+|m['\u2019]|mir\s+|uns\s+|mi\s+|ci\s+)?)?"
)

# The command itself, at the start (after _NOTE_PREFIX). What follows it is the note.
_NOTE_COMMANDS = (
	# fr — « crée-moi une note », « fais une petite note », « ouvre une nouvelle note »
	r"(?:cr[ée]e[rz]?|fai(?:s|t|re|tes)|ajout(?:e|er|ez)|r[ée]dig(?:e|er|ez)|[ée]cri(?:s|re|vez)|"
	r"prend(?:s|re)|prenez|met(?:s|tre|tez)|enregistr(?:e|er|ez)|gard(?:e|er|ez)|ouvr(?:e|ir|ez))"
	r"(?:[- ](?:moi|nous))?\s+(?:une|la|cette|ma)\s+(?:(?:petite|nouvelle|courte|br[eè]ve)\s+)?note\b"
	r"(?:\s+(?:pour\s+(?:moi|nous)|dans\s+mes\s+notes))?",
	# fr — « prends note », « prends ça en note », « note-moi que », « note bien que »
	r"(?:prend(?:s|re)|prenez|prenons)\s+(?:(?:[çc]a|cela|ceci)\s+)?en\s+note\b",
	r"(?:prend(?:s|re)|prenez|prenons)\s+note\b",
	r"note[rz]?(?:[- ](?:moi|nous))?(?:\s+bien)?"
	r"(?=\s*(?::|que\b|qu['\u2019]|de\b|d['\u2019]|pour\b|sur\b|concernant\b|[àa]\s+propos\b))",
	# fr — « nouvelle note : », « une note : », « note : »
	r"(?:une\s+)?(?:nouvelle\s+)?note\s*(?=:)|(?:une\s+)?nouvelle\s+note\b",
	# de — « erstelle mir eine Notiz », « leg eine neue Notiz an », « notiere: », « schreib dir auf »
	r"(?:erstell(?:e|en)?|mach(?:e|en)?|schreib(?:e|en)?|leg(?:e|en)?|notier(?:e|en)?)"
	r"\s+(?:mir\s+|uns\s+|dir\s+)?(?:eine|die|ne)\s+(?:(?:neue|kurze|kleine)\s+)?notiz\b(?:\s+an\b)?",
	r"notier(?:e|en)?(?:\s+(?:dir|mir|uns))?(?=\s*(?::|,?\s*dass\b))",
	r"schreib(?:e)?\s+(?:dir|mir|uns)\s+auf\b",
	r"(?:neue\s+)?notiz\s*(?=:)|neue\s+notiz\b",
	# it — « creami una nota », « prendi nota », « annota che », « nuova nota : »
	r"(?:crea(?:mi|re)?|fa(?:i|mmi)|scriv(?:i|imi|ere)|prendi|aggiungi|apri)"
	r"\s+(?:una|la)\s+(?:(?:nuova|breve|piccola)\s+)?nota\b",
	r"prend(?:i|ere)\s+nota\b",
	r"annota(?:re|mi)?(?=\s*(?::|che\b))",
	r"(?:nuova\s+)?nota\s*(?=:)|nuova\s+nota\b",
	# en — « create a note », « take a note », « take note that », « note that », « new note: »
	r"(?:create|make|take|add|write|start|open)\s+(?:me\s+|us\s+)?(?:a|the|an)\s+(?:(?:new|quick|short|little)\s+)?note\b",
	r"take\s+note\b",
	r"jot\s+(?:this\s+|that\s+|it\s+)?down\b",
	r"note\s+(?:that|down)\b",
	r"(?:new\s+)?note\s*(?=:)|new\s+note\b",
)
_NOTE_COMMAND_RE = re.compile(_NOTE_PREFIX + r"(?:" + "|".join(_NOTE_COMMANDS) + r")", re.IGNORECASE)

# A command that closes the request, the note coming first: « …, note-le », « … mets ça
# dans mes notes », « …, jot it down ».
_NOTE_TRAILING_RE = re.compile(
	r"[\s,;:.—\u2013-]*(?:(?:peux-tu\s+|tu\s+peux\s+|pouvez-vous\s+)?"
	r"(?:(?:note[rz]?|prends|prenez|garde[rz]?)[- ](?:le|la|les|[çc]a|cela|ceci)(?:\s+en\s+note)?"
	r"|(?:mets|mettez|ajoute[rz]?|garde[rz]?)[- ](?:le|la|les|[çc]a|cela|ceci)\s+(?:dans|[àa])\s+mes\s+notes"
	r"|notier(?:e)?\s+(?:das|es|dir\s+das)|annotal[oa]"
	r"|note\s+(?:it|this|that)(?:\s+down)?|jot\s+(?:it|this|that)\s+down|add\s+(?:it|this|that)\s+to\s+my\s+notes))"
	r"\s*[.!]*\s*$",
	re.IGNORECASE,
)

# « … dans mes notes » anywhere: the note is whatever surrounds it.
_INTO_MY_NOTES_RE = re.compile(
	r"\b(?:dans|[àa])\s+mes\s+notes\b|\bin\s+meine\s+notizen\b|\bnelle\s+mie\s+note\b|\b(?:to|in)\s+my\s+notes\b",
	re.IGNORECASE,
)


def _is_note_request(msg: str) -> bool:
    """A request to write a note: never an expense, credit, delivery or fee note, never a reminder
    at a moment (« note-moi de rappeler X vendredi » is a one-off reminder)."""
    text = msg or ""
    if _NOT_A_NOTE_RE.search(text) or _is_one_off_reminder(text):
        return False
    return bool(_NOTE_COMMAND_RE.search(text) or _NOTE_TRAILING_RE.search(text) or _INTO_MY_NOTES_RE.search(text))


# « Que voulez-vous que je note ? »: the next message of the conversation is the note itself.
import time as _time_note

_PENDING_NOTE: dict = {}
_PENDING_NOTE_TTL = 600  # seconds the question stays open
_NOTE_CANCEL_RE = re.compile(
    r"^\s*(?:non\b|nan\b|laisse[rz]?\s+tomber|annule[rz]?\b|rien\b|oublie[rz]?\b|pas\s+maintenant|no\b"
    r"|nein\b|nichts\b|vergiss|niente\b|lascia\s+perdere|annulla\b|never\s*mind|cancel\b|nothing\b)",
    re.IGNORECASE,
)
# //// Neoffice — a bare acknowledgment (« oui », « ok », « d'accord ») says nothing about WHAT to
# //// note: the question stays open for the sentence that does. NORA Live hands a bare « oui » to the
# //// pole that asked last, and it became a note reading « Oui ».
_NOTE_BARE_ACK_RE = re.compile(
    r"^\s*(?:(?:oui|ouais|ok(?:ay)?|d['\u2019]accord|bien\s+s[uû]r|vas[- ]y|allez[- ]y|volontiers|merci"
    r"|yes|yeah|yep|sure|please|thanks|ja|jawohl|gerne|klar|danke|bitte|s[iì]|certo|va\s+bene|grazie)"
    r"[\s,.!]*)+$",
    re.IGNORECASE,
)


def _take_pending_note(conversation_id: Optional[str], message: str) -> bool:
    """True when this message answers NORA's « Que voulez-vous que je note ? »: it is the note.
    The question is consumed either way: a refusal, a question or a request of its own is not."""
    if not conversation_id:
        return False
    asked_at = _PENDING_NOTE.pop(conversation_id, None)
    if asked_at is None or _time_note.time() - asked_at > _PENDING_NOTE_TTL:
        return False
    msg = (message or "").strip()
    if _NOTE_BARE_ACK_RE.match(msg):  # //// Neoffice — nothing said yet: the question stays open
        _PENDING_NOTE[conversation_id] = asked_at
        return False
    if not msg or _NOTE_CANCEL_RE.match(msg) or msg.endswith("?"):
        return False
    return not (_is_note_request(msg) or _is_one_off_reminder(msg) or _RECUR_RE.search(msg))


# Handed to the orchestrator when nora could not write the note in code (no desk callback,
# nora declined or did not answer): it holds nora_note_create.
_NOTE_HINT = (
    "[Route: the person asked to write a NOTE in their Neoffice notes. Call nora_note_create "
    "yourself, now (text=the note in their own words, user and conversation_id as given); do NOT "
    "call kanban_create. Then say back its ack. If the call fails, say so: never answer that it is "
    "noted when nothing was written.]"
)
# //// END Neoffice ////


# //// Neoffice — composing a person's SPACE (its tabs, its name, its icon) is NORA's own
# //// conversation (step 3 of the spaces composed with NORA, 02.10): her server holds the tools, and
# //// a change is kept only after the person's yes to the bar shown (checked in code by nora). The
# //// words of such a request name documents (« ajoute les bons de livraison à mon espace
# //// Commercial ») and would send it to the pole of the document; recognised here, it goes to NORA
# //// with _SPACE_HINT, and the conversation stays hers for 10 minutes (« oui », « et cache aussi
# //// les prospects »). Never the customer portal (« espace client ») nor disk or storage space.
_SPACE_NOUN_RE = re.compile(
    r"\b(?:espaces?(?!\s+(?:clients?|disques?|de\s+stockage|publicitaires?|libres?))(?:\s+de\s+travail)?"
    r"|workspaces?|barre\s+d['\u2019]onglets|(?:mes|tes|vos|nos)\s+onglets"
    r"|arbeitsbereich\w*|spazio\s+di\s+lavoro|(?:my|this|the|a)\s+(?:work)?space)\b",
    re.IGNORECASE,
)
_SPACE_VERB_RE = re.compile(
    r"\b(?:r?ajout\w*|met[st]?|mettre|mettez|ret(?:ire|irer|irez)|enl[eè]v\w*|supprim\w*|cach\w*|masqu\w*"
    r"|affich\w*|remet\w*|renomm\w*|appell?\w*|nomm\w*|chang\w*|modifi\w*|r[ée]organis\w*|organis\w*"
    r"|compos\w*|cr[ée]\w*|personnalis\w*|configur\w*|d[ée]plac\w*|premi[eè]re?|d['\u2019]abord|ic[oô]nes?"
    r"|propos\w*|sugg[eè]r\w*|hinzuf\w*|f[üu]g\w*|entfern\w*|ausblend\w*|umbenenn\w*|erstell\w*"
    r"|aggiung\w*|rimuov\w*|nascond\w*|rinomin\w*|crea\w*|add|remove|hide|show|rename|create|customi[sz]e"
    r"|reorder)\b",
    re.IGNORECASE,
)
_PENDING_SPACE: dict = {}
# //// Neoffice — conversations where nora asked a question about a space (the bar shown, which space):
# //// their next turn goes to nora whatever its words (« Commercial », « oui »).
_SPACE_ASKED: set = set()
_PENDING_SPACE_TTL = 600  # seconds a space conversation stays NORA's


def _is_space_request(msg: str) -> bool:
    """A request to compose one of the person's spaces: a space named, and what to do with it."""
    text = (msg or "").replace(chr(0x2019), "'")
    return bool(_SPACE_NOUN_RE.search(text) and _SPACE_VERB_RE.search(text))


def _continues_space(conversation_id: Optional[str], message: str) -> str:  # //// Neoffice — says why (02.10)
    """Why a turn belongs to a space conversation already under way: "words" (the yes, a refusal, a
    further change), "answer" (it only answers the question nora asked: « Commercial »), or "" (it
    does not: any other turn ends the conversation, and is routed as usual)."""
    if not conversation_id or conversation_id not in _PENDING_SPACE:
        return ""  # //// Neoffice — no space conversation under way
    started = _PENDING_SPACE.pop(conversation_id)
    asked = conversation_id in _SPACE_ASKED  # //// Neoffice — nora asked a question about it
    _SPACE_ASKED.discard(conversation_id)
    msg = (message or "").strip().replace(chr(0x2019), "'")
    if _time_note.time() - started > _PENDING_SPACE_TTL or not msg or len(msg) > 200:
        return ""  # //// Neoffice — expired, empty or a whole new request
    if (  # //// Neoffice — the yes, a refusal, a further change
        _CONFIRM_SEND_RE.search(msg)
        or _NOTE_CANCEL_RE.match(msg)
        or _SPACE_VERB_RE.search(msg)
        or _SPACE_NOUN_RE.search(msg)
    ):
        return "words"
    return "answer" if asked else ""  # //// Neoffice — only the answer to nora's question


# //// Neoffice — the ATELIER (the theme's full screen to compose a space, 02.10). Its NORA box sends every
# //// message with the page context {atelier: {space, label, scope}}, and all of it is about that space,
# //// whatever its words: « Masque les abonnements, je ne m'en sers pas » went to the Support pole, which
# //// answered that it could not hide a module. nora reads it in code and only PROPOSES (a dry run the
# //// atelier shows, kept or undone change by change there); what nora does not read goes to the
# //// orchestrator with _ATELIER_HINT.
def _atelier_of(page_context: Optional[dict]) -> Optional[dict]:
    """The space and level the atelier composes, from the page context; None outside the atelier."""
    atelier = page_context.get("atelier") if isinstance(page_context, dict) else None
    if not isinstance(atelier, dict):
        return None
    space = str(atelier.get("space") or "").strip()
    if not space or len(space) > 140:
        return None
    return {
        "space": space,
        "label": (str(atelier.get("label") or "").strip() or space)[:140],
        "scope": (str(atelier.get("scope") or "").strip() or "user")[:160],
    }


_ATELIER_HINT = (
    "[Route: the person writes from the ATELIER of their space « {label} » (space={space}, scope={scope}): they "
    "compose it on screen, keep or undo each change there, then save. Propose with nora_space_compose(space="
    "{space}, scope={scope}) WITHOUT confirmed, never with it: the atelier shows your proposal. Tabs: add, hide, "
    "show, rename, first. The overview's widgets, by their title (nora_space_widgets gives them and the library "
    "of charts): add_widget, hide_widget, show_widget, size {{widget: S, M or L}}, first_widget; « mets en avant » "
    # //// Neoffice — a space just made (« Composer avec Nora »): its activity, described in plain words
    "is first_widget, after add_widget when the chart is not there yet. A space just made has no list yet: "
    "their message describes the activity, so propose its lists (nora_space_suggest(space, query=<one key "
    "word>), a word at a time) and a name (title). Then say in ONE sentence what you "
    "propose (« Je vous propose de … : gardez-le dans l'atelier si cela vous convient. »). Your space tools hold "
    "all you need: do NOT search the wiki or your memory, do NOT call kanban_create.]"
)
# //// END Neoffice ////


# //// Neoffice — a plain space request is read in code by nora (space_route.route_space, 02.10):
# //// given the tools and the instruction, the orchestrator read « Ajoute les bons de livraison à mon
# //// espace Commercial » as a wiki question, searched it twenty times and was stopped by the guard
# //// (osiris, 13:47). nora reads it, shows the bar with « Voulez-vous que je l'applique ? », and keeps
# //// it on the person's « oui », forwarded here as their next turn. Same desk callback, token and
# //// reply envelope as _route_note; what nora does not read goes to the orchestrator.
def _route_space(
    message: str,
    chat_user: Optional[str],
    deliver_extra: Optional[dict],
    conversation_id: Optional[str],
    follow_up: bool = False,
    page_context: Optional[dict] = None,
) -> dict:
    extra = deliver_extra or {}
    cb = (extra.get("callback_url") or "").strip()
    token = (extra.get("callback_token") or "").strip()
    user = (chat_user or "").strip()
    _DELIVER = "nora.api.v2.hermes_callback.deliver"
    _ROUTE = "nora.api.v2.space_route.route_space"
    declined = {"routed": False, "category": "DIRECT", "ack": None, "task_id": None}
    if not (cb and token and user) or _DELIVER not in cb:
        return declined
    import json as _json
    import urllib.request

    # //// Neoffice — the space on screen helps nora find the one meant (« ajoute … à mon espace »)
    cid = (extra.get("conversation_id") or conversation_id or "").strip()
    page_route = (page_context or {}).get("route") if isinstance(page_context, dict) else None
    req = urllib.request.Request(
        cb.replace(_DELIVER, _ROUTE),
        data=_json.dumps({"user": user, "message": message, "conversation_id": cid,
                          "follow_up": bool(follow_up), "page_route": page_route or "",
                          "atelier": _atelier_of(page_context)}).encode(),  # //// Neoffice — see _atelier_of
        method="POST", headers={"X-Hermes-Token": token, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = _json.loads(resp.read().decode() or "{}")
    except Exception as exc:  # noqa: BLE001  //// Neoffice — nora unreachable: the orchestrator
        logger.warning("nora_chat_router: space POST failed → agent fallback: %s", exc)
        return declined
    if isinstance(data, dict) and isinstance(data.get("message"), dict):
        data = data["message"]  # a whitelisted Frappe method answers {"message": {...}}
    if not isinstance(data, dict) or not data.get("ok") or not data.get("ack"):
        logger.info("nora_chat_router: space not read in code (%s) → agent", (data or {}).get("error"))
        return declined
    delivered = _post_ack_to_callback(data["ack"], deliver_extra)
    if conversation_id and data.get("asked"):
        if len(_SPACE_ASKED) > _LAST_ROUTE_MAX:
            _SPACE_ASKED.clear()
        _SPACE_ASKED.add(conversation_id)
    logger.info("nora_chat_router: space %s read in code (asked=%s, applied=%s, proposed=%s), ack_delivered=%s",
                data.get("space"), bool(data.get("asked")), bool(data.get("applied")),
                bool(data.get("proposed")), delivered)  # //// Neoffice — proposed: the atelier's dry run
    return {"routed": True, "category": "DIRECT", "ack": data["ack"], "task_id": None,
            "space": data.get("space"), "asked": bool(data.get("asked")),
            "applied": bool(data.get("applied")), "ack_delivered": delivered}


_SPACE_HINT = (  # //// Neoffice — the question is one the capability bench recognises; no wiki (13:47)
    "[Route: the person is composing one of their SPACES (tabs, name, icon). It is yours, with your "
    "space tools, which hold all you need: do NOT search the wiki or your memory for it. nora_spaces_list (which space), nora_space_suggest (what goes with it, and why), "
    "nora_space_icons, then nora_space_compose WITHOUT confirmed: show the bar it returns and ask "
    "« Voulez-vous que je l'applique ? ». Only after their yes, the same changes with confirmed=true. A "
    "trade for the whole company (nora_space_trades) is for its administrator only, the same way, saying "
    "it changes every space for everyone. Do NOT call kanban_create. Never say a space changed unless "
    "the call returned confirmed: true.]"
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

# //// Neoffice — a QUESTION asking for information is new intent, never a go-ahead (#1268).
# //// « Ok. Est-ce qu'il y a des rappels à faire ? » matched _CONFIRM_SEND_RE on its « Ok »
# //// and stayed on the previous pole (sales, from the page), where a worker took 35 s. A
# //// confirmation answers a proposal (« oui, envoie », « vas-y », « est-ce qu'on peut
# //// l'envoyer ? »); « is there », « how many », « which » ask for something new, and the
# //// classifier reads them with the conversation in hand. A wh-word counts only with a
# //// question mark: « c'est bon, quand tu veux » is a go-ahead.
_INFO_QUESTION_RE = re.compile(
    r"\b(?:est[- ]ce\s+qu['’]?\s*(?:il|on)\s+(?:y\s+a|a)\b|y\s+a[- ]t[- ]il\b|is\s+there\b|are\s+there\b|"
    r"gibt\s+es\b|ci\s+sono\b)"
    r"|(?=[^?]*\?)\b(?:combien|quel(?:le)?s?|lesquel(?:le)?s|o[uù]\s+en\s+(?:est|sont)|pourquoi|quand|"
    r"how\s+(?:many|much)|which|what|why|when|wie\s*viele?|welche[mnrs]?|warum|wann|quant[ie]|quale|"
    r"quali|perch[ée]|quando)\b",
    re.IGNORECASE,
)
# //// END Neoffice ////


# //// Neoffice — « quel rapport pour … ? », « où je trouve … ? », « dans quel menu … ? » (09.10): a
# //// question about WHERE something lives in Neoffice. Its answer fits in one sentence, so the
# //// classifier may call it 'direct', but on the chat the orchestrator holds no map of Neoffice (no
# //// frappe tool, by design: configure.sh), while every pole reads it (neoffice_feature_map). A
# //// 'direct' verdict on one is asked again in route_chat_message.
_WHERE_TO_FIND_RE = re.compile(
    r"\bquel(?:le)?s?\s+(?:rapports?|[ée]crans?|menus?|modules?|onglets?|espaces?|tableaux?\s+de\s+bord)\b"
    r"|\bdans\s+quel(?:le)?s?\s+(?:menu|module|[ée]cran|onglet|espace|page)\b"
    r"|\bo[uù]\s+(?:est[-\s]ce\s+qu(?:e\s+|['’]\s*))?(?:je\s+|j['’]|on\s+|l['’]on\s+)?"
    r"(?:peux\s+|peut\s+|pourrais\s+|dois\s+|doit\s+)?(?:trouv|voi[rst]\b|consult|retrouv|regard)"
    r"|\bo[uù]\s+se\s+trouve(?:nt)?\b"
    r"|\bwhere\s+(?:can|do|could|should)\s+(?:i|we)\s+(?:find|see|look)\b|\bwhich\s+(?:report|screen|menu)\b"
    r"|\bwo\s+(?:finde|sehe)\s+ich\b|\bwelche[rs]?\s+(?:bericht|auswertung)\b"
    r"|\bdove\s+(?:trovo|vedo)\b|\bquale\s+(?:report|rapporto)\b",
    re.IGNORECASE,
)
# //// END Neoffice ////

# //// Neoffice — a NAME typed alone (« Atelier Démo SA », « Marc Exemple », « Boulangerie du Lac ») is never
# //// DIRECT (09.10). The classifier called the bare customer name 'direct' on the development instance: on
# //// 04.10 the orchestrator answered « Je n'ai pas réussi à traiter votre demande » after 11 s, on 09.10 it
# //// took 22.7 s to hand it to the job pole (answer at 71.6 s). The person wants the record of what they
# //// named, which a pole holds. Asked again with 'direct' excluded; ventes when the classifier still cannot
# //// choose. A name here: up to six words, each capitalised but for connectors, and a company suffix or at
# //// least two capitalised words. One word alone (« Zephyra », « Parfait ») and courtesy (« Merci
# //// Beaucoup ») stay as they were.
_COMPANY_SUFFIX_RE = re.compile(r"^(?:SA|S\.A\.?|S[àa]rl|SARL|AG|GmbH|Sagl|SNC|Cie|Ltd|Inc|SAS|SRL)\.?$")
_NAME_CONNECTORS = frozenset({"de", "du", "des", "la", "le", "les", "et", "&", "von", "van", "der", "di", "da", "y"})
_ELIDED_CONNECTOR_RE = re.compile(r"^[dlDL]'")  # « de l'Etang », « D'Amico »: the name starts after the apostrophe
_NOT_A_NAME_WORDS = frozenset(
    "bonjour salut bonsoir coucou hello hey hi yo ciao hallo merci beaucoup bon bonne journée journee soirée soiree "
    "nuit année annee joyeux noël noel anniversaire félicitations felicitations bravo parfait super top génial "
    "genial cool excellent oui non ok okay d'accord très tres bien au revoir rien pardon désolé desole nora c'est "
    "voici voilà voila test thanks thank you good morning evening bye danke guten tag morgen grazie buongiorno "
    "buonasera".split())
_BARE_NAME_REASON = (
    "[Ce message n'est qu'un nom (client, fournisseur, personne, article ou chantier) : la personne veut sa fiche. "
    "'direct' est exclu ; un client ou un fournisseur → ventes, un employé → rh, un chantier → projet.]"
)


def _is_bare_name(msg: str) -> bool:
    """True when the whole message is a name: « Atelier Démo SA », « Marc Exemple », « Boulangerie du Lac »."""
    text = (msg or "").strip().rstrip("?!.").strip()
    words = text.replace("’", "'").split()
    if not 1 <= len(words) <= 6 or len(text) > 60:
        return False
    capitals = suffixes = 0
    for word in words:
        if word.lower() in _NOT_A_NAME_WORDS:
            return False
        if _COMPANY_SUFFIX_RE.match(word):
            suffixes += 1
        elif word.lower() not in _NAME_CONNECTORS:
            if not _ELIDED_CONNECTOR_RE.sub("", word)[:1].isupper():
                return False  # a lower-case word: a sentence, not a name
            capitals += 1
    return capitals >= 1 and (suffixes >= 1 or capitals >= 2)
# //// END Neoffice ////


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
        # //// Neoffice — a question for information, or a personal reminder to set, is
        # //// not the confirmation of a proposal (#1268): see _INFO_QUESTION_RE.
        and not _INFO_QUESTION_RE.search(msg)
        and not _is_one_off_reminder(msg)
        # //// END Neoffice ////
    ):
        _kw_pole = next((p for rx, p in _FAST_PATH_RULES if rx.search(msg)), None)
        # //// Neoffice — « tu peux la valider puis envoyer le mail » (07.10): see _validation_stays.
        # //// « oui, relance-la » after an rh offer (07.10): see _rh_relance_stays.
        if (
            _kw_pole in (None, prior["pole"])
            or _validation_stays(msg, prior["pole"], _kw_pole)
            or _rh_relance_stays(msg, prior["pole"], _kw_pole)
        ):
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
            # //// Neoffice — a validation with its mail, asked at length (07.10): see _validation_stays.
            if _validation_stays(msg, prior.get("pole"), _explicit):
                logger.info(
                    "nora_chat_router: validating a document stays on %s (the e-mail rule said support)",
                    prior.get("pole"),
                )
                return prior["pole"]
            # //// END Neoffice ////
            # //// Neoffice — a « relance » answering an rh offer, asked at length (07.10): see _rh_relance_stays.
            if _rh_relance_stays(msg, prior.get("pole"), _explicit):
                logger.info("nora_chat_router: a relance answering an rh offer stays on rh (the dunning rule said compta)")
                return prior["pole"]
            # //// END Neoffice ////
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
    # //// Neoffice — recruitment's documents (07.10, see _RECRUITMENT_RE).
    "Job Applicant": "rh", "Job Opening": "rh", "Job Offer": "rh", "Job Requisition": "rh",
    "Interview": "rh", "Interview Feedback": "rh", "Employee Referral": "rh", "Appointment Letter": "rh",
    "Staffing Plan": "rh",
    "Project": "projet", "Task": "projet", "Timesheet": "projet",
    "Issue": "support", "HD Ticket": "support",
}
# //// END Neoffice ////


# //// Neoffice — a message that points at THE document on screen goes to that document's
# //// pole, before the classifier (#914). On 28.09 « Résume ce document » sent from a
# //// journal entry was classified « support » in 2.8 s: a cold support worker answered
# //// « lequel ? » after 57 s, while the page said compta. The page's pole was only a
# //// fallback for a classifier that failed; a demonstrative (« ce document », « cette
# //// écriture », « this invoice », « dieses Dokument », « questo documento ») says the
# //// page is the subject, so the page decides. Four languages, like the rest of NORA.
_POINTS_AT_THE_PAGE_RE = re.compile(
    r"\b(?:"
    r"(?:ce|cet|cette|ces)\s+(?:document|documents|pi[eè]ce|[eé]criture|facture|devis|offre|commande|"
    r"bon|livraison|paiement|r[eè]glement|note|fiche|dossier|ticket|projet|t[aâ]che|demande|contrat|"
    r"relance|client|fournisseur|article|employ[eé]|cong[eé])"
    r"|(?:le|la)\s+(?:document|page|fiche|pi[eè]ce)\s+(?:ouvert|ouverte|affich[eé]e?|en\s+cours)"
    r"|(?:this|these)\s+(?:document|documents|entry|invoice|quote|quotation|offer|order|delivery|payment|"
    r"record|note|ticket|project|task|claim|request|contract|customer|supplier|item|page)"
    r"|(?:dieses|diese|diesen|dieser)\s+(?:dokument|buchung|rechnung|angebot|auftrag|lieferung|zahlung|"
    r"beleg|ticket|projekt|aufgabe|antrag|vertrag|kunden|lieferanten|artikel|seite)"
    r"|(?:questo|questa|questi|queste)\s+(?:documento|documenti|registrazione|fattura|offerta|preventivo|"
    r"ordine|consegna|pagamento|ticket|progetto|attivit[aà]|richiesta|contratto|cliente|fornitore|"
    r"articolo|pagina)"
    r")\b",
    re.IGNORECASE,
)


def _page_document_pole(message: str, page_doctype: Optional[str]) -> Optional[str]:
    """The pole of the document open on the page when the message points at it, else None."""
    pole = _DOCTYPE_POLES.get(page_doctype or "")
    if pole in POLES and _POINTS_AT_THE_PAGE_RE.search(message or ""):
        return pole
    return None
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
    exclude_direct: bool = False,  # //// Neoffice — asked again for a pole, see _WHERE_TO_FIND_RE
    exclude_reason: Optional[str] = None,  # //// Neoffice — why, when not a where-to-find (see _is_bare_name)
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
    # //// Neoffice — asked again for a pole: the rules and the page already answered (exclude_direct)
    fast = None if exclude_direct else _fast_path(msg, prior)
    if fast:
        logger.info("nora_chat_router: keyword fast-path → %s (no LLM call)", fast)
        return fast
    # //// Neoffice — the pole the calling page already chose (#681, 2026-09-24). The NORA
    # //// Live page escalates with the pole its voice server named, and that server has
    # //// ALREADY said it aloud (« je transmets aux ventes… »). Without the hint the
    # //// classifier decided again, and the ack could name one pole while another worked.
    # //// After the deterministic rules, which stay the only thing above the page's word.
    if hint in POLES and not exclude_direct:
        logger.info("nora_chat_router: page pole hint → %s (no LLM call)", hint)
        return hint
    # //// END Neoffice ////
    # //// Neoffice — « ce document » on a page with a document open: the page decides (#914).
    page_pole = None if exclude_direct else _page_document_pole(msg, page_doctype)
    if page_pole:
        logger.info("nora_chat_router: the message points at the page's %s → %s (no LLM call)",
                    page_doctype, page_pole)
        return page_pole
    # //// END Neoffice ////
    user_content = msg[:2000]
    if prior and prior.get("pole"):
        user_content = (
            f"[Contexte conversation — message précédent : « {(prior.get('msg') or '')[:200]} » "
            f"→ pôle « {prior['pole']} ». Si ce nouveau message est une suite/précision de ce "
            f"qui précède, garde le pôle « {prior['pole']} ».]\n{user_content}"
        )
    # //// Neoffice — see _WHERE_TO_FIND_RE: the pole of the subject, never 'direct'
    if exclude_direct:
        user_content = (exclude_reason or (
            "[Ce message demande OÙ trouver quelque chose dans Neoffice : 'direct' est exclu, "
            "réponds par le pôle de son sujet.]")) + "\n" + user_content
    # //// END Neoffice ////
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
        # //// Asked again WITH the list of answers (09.10): for a question about a client's contact the
        # //// model answered an empty string, finish « stop », five times in six at temperature 0, so the same
        # //// question asked again failed again (2 routings of 224 that day, 25 s each through the
        # //// orchestrator). With the one-word reminder it answered the pole six times in six.
        if not raw:
            retry = [messages[0], {"role": "user", "content": (
                f"{user_content}\n[Réponds par un seul mot parmi : {', '.join(sorted(POLES))}, direct.]")}]
            resp = call_llm_fn(
                task="nora_chat_routing",
                messages=retry,
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


# //// Neoffice — page_context added: the voice source of the request rides in the notify subscription's
# //// delivery_metadata, so the ERP worker that answers it keeps the voice priority (-10) instead of 0.
def _add_notify_sub(notify_db, conn, *, conversation_id: Optional[str] = None,
                    page_context: Optional[dict] = None, **kw) -> None:
    """Subscribe the notifier so the worker's terminal result comes back to THIS chat.

    The desk conversation id rides in upstream's ``delivery_metadata`` (a JSON blob the
    notifier copies verbatim into the adapter's send metadata), so no fork column is
    needed: ``webhook.send`` reads ``metadata["conversation_id"]`` to reach the right
    desk thread. Before v2026.9.7 we carried a bespoke ``conversation_id`` column here.
    """
    # //// Neoffice — the metadata is built for every subscription (not only a desk one): it also carries
    # //// the allowlisted voice source below.
    metadata = dict(kw.pop("delivery_metadata", None) or {})
    if conversation_id:
        metadata.setdefault("conversation_id", conversation_id)
    # //// Neoffice — the worker cannot recover the voice origin from its prose prompt. Persist only the
    # //// existing voice-source allowlist, never an arbitrary client priority.
    pc = page_context if isinstance(page_context, dict) else {}
    source = str(pc.get("source") or "").strip()
    metadata["neoffice_request_source"] = source if source in VOICE_SOURCES else ""
    kw["delivery_metadata"] = metadata
    notify_db.add_notify_sub(conn, **kw)


def _post_ack_to_callback(ack: str, deliver_extra: Optional[dict]) -> bool:
    """Deliver the immediate ack to the desk via the nora callback — the SAME POST the
    nora-deliver path uses ({conversation_id, text} + X-Hermes-Token). We POST it here
    rather than via the platform's ``_direct_deliver`` because that routes the custom
    "nora" deliver type to ``_deliver_cross_platform``, which doesn't know it, so the
    ack was silently dropped (success=False, no exception). A WhatsApp chat has no
    callback_url: its text is queued for the central WhatsApp router instead (True means
    queued there), see _post_to_whatsapp_router. Never raises: a failed ack must not break
    routing."""
    extra = deliver_extra or {}
    url = (extra.get("callback_url") or "").strip()
    token = (extra.get("callback_token") or "").strip()
    cid = (extra.get("conversation_id") or "").strip()
    # //// Neoffice — a WhatsApp chat (09.10), see _post_to_whatsapp_router.
    if not url:
        return _post_to_whatsapp_router(ack, extra)
    # //// END Neoffice ////
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


# //// Neoffice — the router's own replies reach a WhatsApp chat (09.10). Every reply the router
# //// writes itself (a greeting, a fast answer, the light path, « Pour quand ? », the ack of a task)
# //// went to the desk callback only. A WhatsApp route carries the central router instead
# //// (router_url, router_api_key, phone): the reply was logged « delivered=False » and, the turn
# //// being handled, the agent was skipped, so the person got nothing (dev instance, whatsapp_inbox).
# //// Same text contract as the webhook adapter's _deliver_whatsapp_router; a voice note is the
# //// agent's final reply only, a reply of the router goes as text.
# //// The POST runs on ONE background thread (in order, never in the gateway's event loop, where
# //// the router runs, #869): the central router waits on WhatsApp before it answers, 8 s and more
# //// for a number WhatsApp does not know, and every message of every chat waited with it (the
# //// gate's WhatsApp greeting went from instant to 7 s). A « number » that is not one (a test's
# //// « +417000800T000 ») is never handed to WhatsApp.
from concurrent.futures import ThreadPoolExecutor as _WhatsAppPool

_E164_RE = re.compile(r"^\+?[1-9]\d{7,14}$")
_WHATSAPP_SENDER: Optional[_WhatsAppPool] = None


def _whatsapp_sender() -> _WhatsAppPool:
    global _WHATSAPP_SENDER
    if _WHATSAPP_SENDER is None:
        _WHATSAPP_SENDER = _WhatsAppPool(max_workers=1, thread_name_prefix="nora-whatsapp")
    return _WHATSAPP_SENDER


def _post_to_whatsapp_router(text: str, extra: dict) -> bool:
    """Hand *text* to the central WhatsApp router ({phone, text} on <router_url>/api/sendText,
    Bearer router_api_key), on the background sender. True once queued; False when the route is
    not a whole WhatsApp route or the number is not one. Never raises, never waits."""
    url = (extra.get("router_url") or "").strip()
    key = (extra.get("router_api_key") or "").strip()
    phone = (extra.get("phone") or "").strip()
    if not (url and key and text) or not _E164_RE.match(phone):  # « {phone} »: the payload had none
        return False
    import json as _json
    import urllib.request

    req = urllib.request.Request(
        url.rstrip("/") + "/api/sendText",
        data=_json.dumps({"phone": phone, "text": text}).encode(),
        method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )

    def _send() -> bool:
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                status = getattr(resp, "status", 0)
                logger.info("nora_chat_router: posted to WhatsApp status=%s (%d chars)", status, len(text))
                return 200 <= status < 300
        except Exception as exc:  # noqa: BLE001
            logger.warning("nora_chat_router: WhatsApp router POST failed: %s", exc)
            return False

    try:
        _whatsapp_sender().submit(_send)
    except RuntimeError:  # the interpreter is shutting down
        return False
    return True


def flush_whatsapp_sends(timeout: float = 20.0) -> bool:
    """Wait until every reply handed to the WhatsApp sender has been posted (tests, the gate)."""
    try:
        _whatsapp_sender().submit(lambda: None).result(timeout=timeout)
        return True
    except Exception:  # noqa: BLE001
        return False
# //// END Neoffice ////


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


# //// Neoffice — a note is WRITTEN IN CODE by nora (notes.route_note, #1040), over the desk
# //// callback and token of _route_reminder. nora reads the note out of the request, writes it as
# //// the person and answers; with nothing to note it asks what to note, and the next message of
# //// the conversation comes back here as the note (follow_up). Declined or failed → routed=False
# //// and the orchestrator gets _NOTE_HINT (zero regression).
def _route_note(
    message: str,
    chat_user: Optional[str],
    deliver_extra: Optional[dict],
    conversation_id: Optional[str],
    follow_up: bool = False,
) -> dict:
    extra = deliver_extra or {}
    cb = (extra.get("callback_url") or "").strip()
    token = (extra.get("callback_token") or "").strip()
    user = (chat_user or "").strip()
    _DELIVER = "nora.api.v2.hermes_callback.deliver"
    _ROUTE = "nora.api.v2.notes.route_note"
    declined = {"routed": False, "category": "DIRECT", "ack": None, "task_id": None}
    if not (cb and token and user) or _DELIVER not in cb:
        return declined
    import json as _json
    import urllib.request

    cid = (extra.get("conversation_id") or conversation_id or "").strip()
    req = urllib.request.Request(
        cb.replace(_DELIVER, _ROUTE),
        data=_json.dumps({"user": user, "message": message, "conversation_id": cid,
                          "follow_up": bool(follow_up)}).encode(),
        method="POST", headers={"X-Hermes-Token": token, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = _json.loads(resp.read().decode() or "{}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("nora_chat_router: note POST failed → agent fallback: %s", exc)
        return declined
    if isinstance(data, dict) and isinstance(data.get("message"), dict):
        data = data["message"]  # a whitelisted Frappe method answers {"message": {...}}
    if not isinstance(data, dict) or not data.get("ok") or not data.get("ack"):
        logger.info("nora_chat_router: note declined (%s) → agent fallback", (data or {}).get("error"))
        return declined
    delivered = _post_ack_to_callback(data["ack"], deliver_extra)
    if data.get("asked") and conversation_id:
        if len(_PENDING_NOTE) > _LAST_ROUTE_MAX:
            _PENDING_NOTE.clear()
        _PENDING_NOTE[conversation_id] = _time_note.time()
    logger.info("nora_chat_router: note %s written in code (asked=%s), ack_delivered=%s",
                data.get("note"), bool(data.get("asked")), delivered)
    return {"routed": True, "category": "DIRECT", "ack": data["ack"], "task_id": None,
            "note": data.get("note"), "asked": bool(data.get("asked")), "ack_delivered": delivered}
# //// END Neoffice ////


# //// Neoffice — an EMPLOYEE's record is rh's, not a client's (27.09, #843). « <Prénom Nom> a
# //// déménagé : sa nouvelle adresse est … Mets sa fiche à jour » matches the contact rules
# //// word for word and reached ventes, which searched clients and suppliers seventeen times
# //// and asked for the spelling: the person was an employee, whose address rh writes
# //// (hr_employee_update). Words cannot tell the two apart, so nora looks the name up
# //// (task_router.employee_named), over the desk callback and token of _route_reminder.
# //// Asked only for a contact change already routed to ventes; any failure keeps ventes.
def _employee_named(message: str, deliver_extra: Optional[dict]) -> bool:
    extra = deliver_extra or {}
    cb = (extra.get("callback_url") or "").strip()
    token = (extra.get("callback_token") or "").strip()
    _DELIVER = "nora.api.v2.hermes_callback.deliver"
    _ROUTE = "nora.api.v2.task_router.employee_named"
    if not (cb and token) or _DELIVER not in cb:
        return False
    import json as _json
    import urllib.request

    req = urllib.request.Request(
        cb.replace(_DELIVER, _ROUTE),
        data=_json.dumps({"message": message}).encode(),
        method="POST", headers={"X-Hermes-Token": token, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = _json.loads(resp.read().decode() or "{}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("nora_chat_router: employee lookup failed → ventes kept: %s", exc)
        return False
    if isinstance(data, dict) and isinstance(data.get("message"), dict):
        data = data["message"]  # a whitelisted Frappe method answers {"message": {...}}
    return bool(isinstance(data, dict) and data.get("ok") and data.get("employee"))


def _pole_for_an_employee_record(category: str, message: str, deliver_extra: Optional[dict]) -> str:
    """rh when a contact change routed to ventes names one of the company's employees."""
    msg = message or ""
    if (
        category == "ventes"
        and (_CONTACT_CHANGE_RE.search(msg) or _CONTACT_FACT_RE.search(msg))
        and _employee_named(msg, deliver_extra)
    ):
        logger.info("nora_chat_router: ventes → rh (the contact change names an employee)")
        return "rh"
    return category
# //// END Neoffice ////


# //// Neoffice — DETERMINISTIC FAST-PATH engine call. Ask the nora fast_answer engine
# (ONE structured-intent LLM call + a Frappe query in CODE) over the SAME desk callback
# the ack uses (X-Hermes-Token), exactly like _route_recurrent. Returns the answer text
# on a confident hit, else None → the caller routes to a worker (zero regression).
# grep "//// Neoffice".
# //// Neoffice — which turns ask nora's fast-answer engine. It ran for French only: its
# //// sentences were French. Since 2026-09-27 (nora #867) the engine answers in the language
# //// of the question, all four of them, and nora tells the gateway the message's language;
# //// a question asked in English or German no longer waits for a worker it did not need.
_FAST_ANSWER_LANGUAGES = frozenset({"fr", "de", "it", "en"})


def _asks_fast_answer(language: Optional[str], canned_text: Optional[str], one_off_reminder: bool) -> bool:
    """True unless the turn is canned small talk or a one-off reminder, both answered elsewhere."""
    return _norm_lang(language) in _FAST_ANSWER_LANGUAGES and not canned_text and not one_off_reminder
# //// END Neoffice ////


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
    nora_spoke: Optional[bool] = None,  # //// Neoffice — NORA already replied in this thread (nora's chat log), #1065 ////
    help_request: Optional[str] = None,  # //// Neoffice — nora read a request for help in code (« ask »), #1174 ////
) -> dict:
    """Classify *message* and, when it is a business request, create the kanban task
    in code + subscribe the notifier. Returns a decision dict::

        {"routed": bool, "category": str, "ack": Optional[str], "task_id": Optional[str]}

    ``routed=False`` (category ``DIRECT`` or any creation failure) means the caller
    MUST proceed with the normal agent dispatch — the message is never dropped.
    """
    # //// Neoffice — first, so a message handed on to the orchestrator still tells its task the
    # //// person's language (see worker_reply_directive).
    remember_chat_language(session_chat_id, language)
    # //// END Neoffice ////
    # //// Neoffice — a WhatsApp chat sends no conversation_id: its thread is the number, see chat_thread.
    if not conversation_id:
        conversation_id = chat_thread(session_chat_id)
    # //// END Neoffice ////
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

    # //// Neoffice — a note for the person asking is written by nora in code (_route_note,
    # //// #1040). Decided before small talk, the fast-answer engine and the classifier: the word
    # //// « chantier » sent « crée une note … » to the projet pole, and neither a job page nor the
    # //// pole the voice suggests may take it. A conversation already on the projet pole keeps
    # //// it when no job page is open: that worker knows the job, and files the note on its diary.
    _note_follow_up = _offer is None and _take_pending_note(conversation_id, message)
    _note_request = _offer is None and (
        _note_follow_up
        or (
            _is_note_request(message)
            and not (prior and prior.get("pole") == "projet" and not _page_project(page_context))
        )
    )
    # //// END Neoffice ////

    # //// Neoffice — a SPACE composed with NORA is hers (step 3, 02.10), see _SPACE_HINT: the
    # //// request, then the turns that follow it (the yes, a further change) for 10 minutes.
    _space_follow = _continues_space(conversation_id, message) if _offer is None and not _note_request else ""
    # //// Neoffice — from the atelier, every turn is about its space, see _atelier_of
    _atelier = _atelier_of(page_context) if _offer is None and not _note_request else None
    _space_turn = bool(_atelier) or bool(_space_follow) or (
        _offer is None and not _note_request and _is_space_request(message)
    )
    _space_routed: dict = {}
    if _space_turn:
        _space_routed = _route_space(message, chat_user, deliver_extra, conversation_id,
                                     follow_up=bool(_space_follow), page_context=page_context)
        if not _space_routed.get("routed") and _space_follow == "answer" and not _atelier:  # //// Neoffice
            # //// Neoffice — it answered nothing nora asked (« combien de factures ? »): routed as usual
            _space_turn = False
    if _space_turn and conversation_id:
        _PENDING_SPACE[conversation_id] = _time_note.time()
    # //// END Neoffice ////

    # //// Neoffice — the person's scheduled tasks are the orchestrator's, see _manages_scheduled_tasks
    _tasks_turn = (
        _offer is None and not _note_request and not _space_turn
        and _manages_scheduled_tasks(message, prior)
    )
    # //// Neoffice — a reminder asked without its moment, see _needs_reminder_moment (09.10)
    _reminder_completed = _take_pending_reminder(conversation_id, message) if _offer is None else None
    _reminder_ask = (
        _offer is None and not _note_request and not _space_turn and not _reminder_completed
        and _needs_reminder_moment(message)
    )
    _owned_turn = bool(_tasks_turn or _reminder_completed or _reminder_ask)  # settled before any pole
    # //// END Neoffice ////

    # //// Neoffice — a yes in a thread where NORA has said nothing confirms nothing, see
    # //// _NOTHING_PROPOSED (#1065): a bare one is answered in code, any other gets a rule.
    _fresh_thread = (
        _offer is None and not _note_request and not _space_turn
        and _thread_is_fresh(conversation_id, nora_spoke)
    )
    _fresh_yes = _fresh_thread and bool(_YES_HEAD_RE.match((message or "").strip()))
    _nothing_to_confirm = _nothing_proposed_reply(message, language) if _fresh_thread else None
    # //// END Neoffice ////

    # //// Neoffice — pure small talk is answered from a template: no classifier, no
    # fast-answer thread, no light-path completion (see _canned_smalltalk_reply).
    _canned_text = (  # //// Neoffice — never for a note (#1040) nor a space turn (step 3)
        (_nothing_to_confirm or _canned_smalltalk_reply(message, language))  # //// Neoffice — #1065
        if _offer is None and not _note_request and not _space_turn and not _owned_turn  # //// Neoffice — 09.10
        else None
    )
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
    if (  # //// Neoffice — never for a note (#1040) nor a space turn (step 3)
        not _note_request
        and not _space_turn
        and not _owned_turn  # //// Neoffice — nor the scheduled tasks, nor a reminder's moment (09.10)
        and _asks_fast_answer(language, _canned_text, _one_off_reminder)
    ):
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
    elif _note_request:
        category = "DIRECT"  # //// Neoffice — a note, written in code (#1040), classifier skipped ////
    elif _space_turn:
        category = "DIRECT"  # //// Neoffice — a space, NORA's own conversation (step 3), classifier skipped ////
    elif _owned_turn:
        category = "DIRECT"  # //// Neoffice — the scheduled tasks, a reminder's moment (09.10) ////
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
        and not _note_request  # //// Neoffice — a note on a job page is a note (#1040)
        and not _space_turn  # //// Neoffice — a space composed on a job page is a space (step 3)
        and not _owned_turn  # //// Neoffice — the scheduled tasks are the orchestrator's (09.10)
        and _page_project(page_context)
        and not _DIRECT_RE.match((message or "").strip())
        and not _CAPABILITY_RE.match((message or "").strip())
    ):
        logger.info("nora_chat_router: DIRECT → projet (job page %s)", _page_project(page_context))
        category = "projet"
    # //// END Neoffice ////
    # //// Neoffice — a request for help is never DIRECT (#1174). nora reads it in code
    # //// (help_intent.read_help_request: « comment faire une note de crédit ? », « aide-moi à saisir… »)
    # //// and says so in the message. The capability rule of the classifier sent such a question to DIRECT,
    # //// as one « answered in a sentence »: the orchestrator then searched its doctrine wiki, which does
    # //// not hold the user manual, 29 model calls in 204 s, and answered that it could not finish (all
    # //// three on osiris over 60 days). The support pole owns help with using Neoffice and answered the
    # //// same question in 19 s. A pole the classifier chose is kept (« comment faire un devis » → ventes),
    # //// and so are the answers settled in code (small talk, a note, a space).
    if (
        category == "DIRECT"
        and help_request == "ask"
        and not (_canned_text or _note_request or _space_turn or _owned_turn)  # //// Neoffice — 09.10
    ):
        logger.info("nora_chat_router: DIRECT → support (a request for help, read by nora)")
        category = "support"
    # //// END Neoffice ////
    # //// Neoffice — « où je trouve … ? », « quel rapport pour … ? » is never DIRECT (09.10), see
    # //// _WHERE_TO_FIND_RE. Sent to the orchestrator, « Quel rapport me permet de suivre ce que mes
    # //// clients me doivent encore ? » called a map it does not hold, then searched its doctrine wiki
    # //// five times with the same words until the guardrail ended the turn on « je n'ai pas réussi »
    # //// (capability bench, 08.10), where the compta pole had named the receivables report from the
    # //// map every night before. Asked once more with 'direct' excluded, the classifier names the
    # //// pole of the subject; support, which owns help with using Neoffice, when it still cannot.
    if (
        category == "DIRECT"
        and _WHERE_TO_FIND_RE.search(message or "")
        and not (_canned_text or _note_request or _space_turn or _owned_turn)
    ):
        again = classify(
            message,
            call_llm_fn=call_llm_fn,
            main_runtime=main_runtime,
            prior=prior,
            timeout=classify_timeout,
            exclude_direct=True,
        )
        category = again if again in POLES else "support"
        logger.info("nora_chat_router: DIRECT → %s (where to find something in Neoffice)", category)
    # //// END Neoffice ////
    # //// Neoffice — a name typed alone is never DIRECT (09.10), see _is_bare_name.
    if (
        category == "DIRECT"
        and _is_bare_name(message)
        and not (_canned_text or _note_request or _space_turn or _owned_turn)
    ):
        again = classify(
            message,
            call_llm_fn=call_llm_fn,
            main_runtime=main_runtime,
            prior=prior,
            timeout=classify_timeout,
            exclude_direct=True,
            exclude_reason=_BARE_NAME_REASON,
        )
        category = again if again in POLES else "ventes"
        logger.info("nora_chat_router: DIRECT → %s (a name typed alone)", category)
    # //// END Neoffice ////
    if not _owned_turn:  # //// Neoffice — the scheduled tasks stay the orchestrator's (09.10)
        category = _pole_for_an_employee_record(category, message, deliver_extra)  # //// Neoffice — #843 ////
    # Remember this turn so the NEXT message resolves a follow-up in context. Track DIRECT
    # too (pole=None) so a follow-up to a greeting doesn't inherit a stale pole.
    if conversation_id:
        if len(_LAST_ROUTE) > _LAST_ROUTE_MAX:
            _LAST_ROUTE.clear()
        _LAST_ROUTE[conversation_id] = {
            "msg": (message or "")[:200],
            "pole": (category if category in POLES else None),
            "title": _task_title(message, prior),  # //// Neoffice — what a later bare yes is titled by ////
            # //// Neoffice — a turn about the scheduled tasks: its follow-up stays with them (09.10)
            "tasks": bool(_tasks_turn or category == "recurrent"),
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
        # //// Neoffice — a space is NORA's own conversation, with its instruction (step 3).
        if _space_turn:
            if _space_routed.get("routed"):  # //// Neoffice — read in code by nora, see _route_space
                note_nora_reply(conversation_id, _space_routed["ack"])
                return _space_routed
            return {"routed": False, "category": "DIRECT", "ack": None, "task_id": None,
                    "agent_hint": _ATELIER_HINT.format(**_atelier) if _atelier else _SPACE_HINT}  # //// Neoffice
        # //// END Neoffice ////
        # //// Neoffice — a note is written in code first, see _route_note (#1040).
        if _note_request:
            _note = _route_note(message, chat_user, deliver_extra, conversation_id, follow_up=_note_follow_up)
            if _note.get("routed"):
                note_nora_reply(conversation_id, _note["ack"])
                return _note
            return {"routed": False, "category": "DIRECT", "ack": None, "task_id": None,
                    "agent_hint": _NOTE_HINT}
        # //// END Neoffice ////
        # //// Neoffice — the reminder NORA asked the moment of, see _needs_reminder_moment (09.10)
        if _reminder_completed:
            _rem = _route_reminder(_reminder_completed, chat_user, deliver_extra)
            if _rem.get("routed"):
                note_nora_reply(conversation_id, _rem["ack"])
                return _rem
            return {"routed": False, "category": "DIRECT", "ack": None, "task_id": None,
                    "agent_hint": f"{_ONE_OFF_REMINDER_HINT} [The reminder asked: « {_reminder_completed} ».]"}
        if _reminder_ask:
            _ask = _REMINDER_WHEN.get(_norm_lang(language), _REMINDER_WHEN["fr"])
            _ask_cid = (deliver_extra or {}).get("conversation_id") or (conversation_id or None)
            _ask_delivered = _post_ack_to_callback(_ask, {**(deliver_extra or {}), "conversation_id": _ask_cid})
            if conversation_id:
                if len(_PENDING_REMINDER) > _LAST_ROUTE_MAX:
                    _PENDING_REMINDER.clear()
                _PENDING_REMINDER[conversation_id] = (_time_note.time(), message)
            note_nora_reply(conversation_id, _ask)
            logger.info("nora_chat_router: a reminder without its moment → asked when (delivered=%s)", _ask_delivered)
            return {"routed": True, "category": "DIRECT", "ack": _ask, "task_id": None,
                    "fast": True, "ack_delivered": _ask_delivered}
        # //// END Neoffice ////
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
                "nora_chat_router: %s (no LLM, %d chars) delivered=%s",
                "a yes in a fresh thread confirms nothing (#1065)" if _nothing_to_confirm  # //// Neoffice
                else "SMALLTALK canned",
                len(_canned_text), _cn_delivered,
            )
            return {
                "routed": True, "category": "DIRECT", "ack": _canned_text,
                "task_id": None, "fast": True, "ack_delivered": _cn_delivered,
            }
        # //// END Neoffice ////
        _is_capability = bool(_CAPABILITY_RE.match(message or ""))
        if not _owned_turn and (_is_capability or (  # //// Neoffice — a task turn is acted on (09.10)
            len(message or "") <= 80
            and _SMALLTALK_RE.search(message or "")
            and not _BUSINESS_RE.search(message or "")
        )):
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
        # //// Neoffice — the orchestrator gets the reminder instruction, see _ONE_OFF_REMINDER_HINT,
        # //// and a yes in a fresh thread its rule, see _FRESH_THREAD_HINT (#1065).
        _direct_hints = [h for h in (_ONE_OFF_REMINDER_HINT if _one_off_reminder else None,
                                     _FRESH_THREAD_HINT if _fresh_yes else None,
                                     _SCHEDULED_TASKS_HINT if _tasks_turn else None) if h]  # //// Neoffice — 09.10
        return {"routed": False, "category": "DIRECT", "ack": None, "task_id": None,
                "agent_hint": "\n".join(_direct_hints) or None}  # //// Neoffice — #1065

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
        # //// Neoffice — a question about the scheduled tasks (nora refuses to create one from it,
        # //// 09.10): the orchestrator answers it with her task tools, never a pole the words name.
        if _rec.get("reason") == "question":
            logger.info("nora_chat_router: a question about the scheduled tasks → orchestrator")
            return {"routed": False, "category": "DIRECT", "ack": None, "task_id": None,
                    "agent_hint": _SCHEDULED_TASKS_HINT}
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
    # //// Neoffice — the time of each step, said when routing is slow (09.10). On the development
    # //// instance a task was created at once and its ack posted 12.6 s later, with nothing logged in
    # //// between (the ack itself took 44 ms at nginx): the next one must name its step.
    _steps = {"start": _time_thread.monotonic()}
    # //// END Neoffice ////
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
            elif _fresh_yes:  # //// Neoffice — a yes in a fresh thread points at nothing (#1065)
                _body = f"{_FRESH_THREAD_HINT}\n\n{message}"
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
            # user turn is always last). With it, the partner guard. Both are built by
            # worker_reply_directive, shared with the orchestrator's kanban_create (08.10).
            # grep "//// Neoffice".
            _body = f"{_body}\n\n{worker_reply_directive(language)}"
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
                title=_task_title(message, prior),  # //// Neoffice — a bare yes, by what it confirms ////
                body=_body,
                assignee=category,
                created_by="nora-chat-router",
                initial_status="running",
                idempotency_key=idempotency_key,
                **_ws_kwargs,  # //// Neoffice — stable worker cwd ////
            )
            _steps["created"] = _time_thread.monotonic()  # //// Neoffice — see _steps ////
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
                page_context=page_context,  # //// Neoffice — carries the voice source (see _add_notify_sub)
            )
            _steps["subscribed"] = _time_thread.monotonic()  # //// Neoffice — see _steps ////
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
    _steps["closed"] = _time_thread.monotonic()  # //// Neoffice — see _steps ////
    # Deliver the immediate ack NOW so the user sees "I'm handing this to <pole>" right
    # away — it also makes the ~10s worker wait feel responsive. Failure to deliver the
    # ack must NEVER fail the routing (the worker result still arrives via the notifier).
    ack_delivered = _post_ack_to_callback(
        ack, {**(deliver_extra or {}), "conversation_id": _unified_cid}
    )
    # //// Neoffice — see _steps
    _steps["acked"] = _time_thread.monotonic()
    if _steps["acked"] - _steps["start"] > 2.0:
        logger.warning(
            "nora_chat_router: slow routing for task %s: create %.1f s, subscription %.1f s, "
            "close %.1f s, ack %.1f s",
            task_id,
            _steps.get("created", _steps["start"]) - _steps["start"],
            _steps.get("subscribed", _steps["start"]) - _steps.get("created", _steps["start"]),
            _steps["closed"] - _steps.get("subscribed", _steps["start"]),
            _steps["acked"] - _steps["closed"],
        )
    # //// END Neoffice ////
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
