"""Strip NORA's internal machinery from anything a customer reads.

# //// Neoffice — added file (no upstream equivalent).
#
# For the customer there is ONE assistant: Nora. The model still emits internal
# vocabulary now and then ("le spécialiste `compta` va traiter…", "ta tâche
# kanban", mem0/hermes/olares/mcp/worker/board), and a reply that names the
# machinery reads like a leak — support then has to explain what a "board" is.
#
# Two call sites share these patterns, and they MUST agree: the webhook delivery
# path (what the customer receives now) and the session-persistence path (what
# the saved transcript shows later). A transcript that disagrees with the chat is
# worse than either alone, which is why the rules live here once instead of being
# copied into both.
#
# Deliberately conservative: only delegation phrasings and technical product
# names, never ordinary words. French, because that is what NORA speaks to Swiss
# SME customers.
#
# Second rule, same chokepoint: a turn whose API calls all failed must not reach the
# customer as upstream's own error literal. See _PROVIDER_FAILURE_RE below (#450).
#
# Drop this module if the assistant is ever taught not to name its own internals.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

_ROLE_RE = re.compile(
    r"\b(le|la|notre|un|une|du|des|aux?)\s+(sp[ée]cialistes?|services?|collègues?|experts?)\b"
    r"[\s`'\"*_:.\-]*(?:de\s+(?:la\s+)?)?[\s`'\"*_:.\-]*"
    # //// Neoffice — `analyse` and `projet` added (20.09). Without them the phrase
    # //// escapes THIS rule and meets _SPECIALIST_RE below, which replaces the bare
    # //// word: « Le spécialiste projet va… » came out « Le équipe projet va… ».
    # //// A list of poles written by hand drifts the day a pole is added — these two
    # //// arrived on 16.09 and nothing here knew.
    r"(compta\w*|ventes?|support|rh|ressources?\s+humaines?|commercial\w*"
    r"|analyses?|projets?)\b[`'\"*]*",
    re.IGNORECASE,
)
_SPECIALIST_RE = re.compile(r"\bsp[ée]cialistes?\b", re.IGNORECASE)
_KANBAN_TASK_RE = re.compile(r"\bt[âa]ches?\s+kanban\b", re.IGNORECASE)
_INTERNALS_RE = re.compile(r"`?\b(kanban|mem0|hermes|olares|mcp|worker|board)\w*\b`?", re.IGNORECASE)
_SPACES_RE = re.compile(r"[ \t]{2,}")

# //// Neoffice — a provider failure is not an answer.
# When every API call of a turn fails, upstream ends the turn with its own literal
# ("API call failed after 3 retries: HTTP 502 — 502 Bad Gateway", agent/turn_recovery.py)
# and the gateway delivers THAT as Nora's reply. Measured on the night of 2026-09-14,
# during the Olares outage (#449): a customer's desk chat showed exactly that line, in
# English, after 12 s (#450). The customer learns nothing, in a language that is not
# theirs, at the worst possible moment — and the delivery chain itself was fine, so the
# only defect is what we let through. One French sentence instead; the technical text
# stays in the logs, where it is read.
_PROVIDER_FAILURE_RE = re.compile(r"\bAPI call failed after \d+ retries?\b", re.IGNORECASE)
PROVIDER_UNAVAILABLE_REPLY = (
    "Le service d'intelligence artificielle est momentanément indisponible. "
    "Réessayez dans quelques minutes ; si cela dure, prévenez votre administrateur."
)


# //// Neoffice — a guardrail message is addressed to the MODEL, not to the customer.
# When a worker repeats the same call, upstream ends the turn with an instruction
# written for the agent ("I stopped retrying <tool> because it hit the tool-call
# guardrail (<code>) after N repeated non-progressing attempts. The last tool result
# explains the blocker; the next step is to change strategy…", run_agent.py
# _toolguard_controlled_halt_response). Measured end-to-end on 2026-09-16: a customer
# asked for a job quotation, the turn was routed to a pole holding no job tool, its
# worker looped, and THAT sentence arrived in the desk chat — in English, naming a
# halt code, telling the reader to change strategy. Same shape as #450: the delivery
# chain was fine, the only defect is what we let through (#476).
#
# Anchored on the fixed half of the template, so it holds whatever the tool name,
# the code or the count. The technical text stays in the logs, where it is read.
# v2026.9.24 REWORDED the template ("I stopped retrying because I kept running <tool>
# N times without making progress… send `continue`…"): the old anchor stopped matching
# and that sentence reached a customer on the dev instance the evening of the rebase.
# Both wordings are anchored; the test builds the text with upstream's own method, so
# the next rewording fails a test instead of reaching a chat.
_GUARDRAIL_HALT_RE = re.compile(
    r"\bhit the tool-call guardrail\b|\bI stopped retrying because I kept running\b", re.IGNORECASE
)
GUARDRAIL_HALT_REPLY = (
    "Je n'ai pas réussi à traiter votre demande jusqu'au bout. "
    "Reformulez-la ou précisez-la ; si cela se reproduit, prévenez votre administrateur."
)

# //// Neoffice — upstream's TURN NOTICES are written for a developer at a CLI.
# Typing a second message while the assistant is working is ordinary impatience, not
# an edge case, and upstream answers it with its own status line: "↪ Redirected
# current run (2 min elapsed, running: frappe_list). I'll adjust using your
# correction." When a turn ends with no answer it says "⚠️ No reply: <reason>",
# and every reason in agent/turn_explainers.py tells the reader to "Send `continue`"
# or to "switch provider" — instructions for whoever runs the agent, addressed to
# somebody who cannot do either.
#
# Measured on 2026-09-17 by llm/25_usage_chains on osiris, with the desk in French:
# both literals arrived in the chat, in English. Same family as #450 and #476 — the
# delivery chain is fine, the only defect is what we let through.
#
# The MEANING is kept (your message was taken into account / I could not finish),
# because a silent chat is worse than an awkward one; the status detail (elapsed
# minutes, iteration progress, the running tool) is machinery and goes. Anchored on
# the fixed head of each template, at the START of the text, so a real answer that
# happens to quote one of these is untouched.
# //// Neoffice — the notices are keyed, not written inline, so REPLIES below can give
# //// each one its four languages. See the note on REPLIES.
_BUSY_NOTICES = (
    (re.compile(r"^\s*⇩?⏩?\s*Steered into current run\b", re.IGNORECASE), "steered"),
    (re.compile(r"^\s*↪?\s*Redirected current run\b", re.IGNORECASE), "redirected"),
    (re.compile(r"^\s*⏳?\s*(Subagent working|Compressing context|Queued for the next turn)\b",
                re.IGNORECASE), "queued"),
    (re.compile(r"^\s*⚡?\s*Interrupting current task\b", re.IGNORECASE), "interrupting"),
)


_NO_REPLY_RE = re.compile(r"^\s*⚠️?\s*No reply\s*:", re.IGNORECASE)

# //// Neoffice — upstream's FAILURE COPY (agent/turn_failure_copy.py, new in v2026.9.24).
# //// Every failed turn now ends on a paragraph written for a developer at a terminal:
# //// "send /retry, or switch models with /model", "run `hermes doctor`", "add a backup
# //// provider with `hermes fallback add`", "Hermes hit repeated errors…". It REPLACED the
# //// "API call failed after N retries" sentence #450 anchored on, so an engine outage
# //// would have reached the customer in English again. Anchored on that command
# //// vocabulary, which no business answer carries (a path such as …/sales-invoice/new
# //// is not a slash command: the slash must follow a space, a bracket or a backtick),
# //// plus "Hermes" as the subject of a sentence — the customer talks to NORA. The test
# //// renders EVERY template of the module, so a copy added upstream is checked too.
_UPSTREAM_FAILURE_COPY_RE = re.compile(
    r"(?:^|[\s(`])/(?:retry|model|new|compress|reasoning)\b"
    r"|`hermes (?:doctor|setup|model|fallback add|auth)\b"
    r"|\bSend `continue`"
    r"|\bHermes (?:was shutting down|hit|couldn't|could not|didn't|did not)\b",
    re.IGNORECASE,
)
# Which of our sentences answers it: the engine could not be reached or refused, or the
# turn itself could not finish.
_PROVIDER_FAILURE_COPY_RE = re.compile(
    r"\bProvider said:|\b\d+ attempts\b|sent back an empty or broken reply|isn't available on"
    r"|rejected (?:the|this) request|refused this request|rejected your (?:sign-in|API key)"
    r"|security certificate|firewall/CDN|usage limit resets",
    re.IGNORECASE,
)
# //// END Neoffice ////
NO_REPLY_REPLY = (
    "Je n'ai pas réussi à aller au bout de ce message. "
    "Reformulez-le ou précisez-le ; si cela se reproduit, prévenez votre administrateur."
)


# //// Neoffice — the customer's language, not always French (20.09). Every sentence this
# //// module hands a customer was a French literal, while POLE_LABELS and the canned
# //// small talk right next door have carried fr/de/it/en for months. A German-speaking
# //// user whose turn failed read a French apology. The module keeps the French constants
# //// as the canonical text (they are imported elsewhere and by the tests) and the table
# //// resolves the other three off them. Unknown or absent language falls back to French,
# //// which is the product default and the behaviour before this change.
REPLIES = {
    "provider_unavailable": {
        "fr": PROVIDER_UNAVAILABLE_REPLY,
        "de": "Der KI-Dienst ist momentan nicht verfügbar. Versuchen Sie es bitte in ein "
              "paar Minuten erneut; falls dies andauert, informieren Sie Ihren Administrator.",
        "it": "Il servizio di intelligenza artificiale non è al momento disponibile. "
              "Riprovi tra qualche minuto; se il problema persiste, avvisi il suo amministratore.",
        "en": "The AI service is temporarily unavailable. Please try again in a few minutes; "
              "if this continues, notify your administrator.",
    },
    "guardrail_halt": {
        "fr": GUARDRAIL_HALT_REPLY,
        "de": "Ich konnte Ihre Anfrage nicht vollständig bearbeiten. Formulieren Sie sie "
              "bitte um oder präzisieren Sie sie; falls dies erneut vorkommt, informieren "
              "Sie Ihren Administrator.",
        "it": "Non ho potuto elaborare completamente la sua richiesta. La riformuli o la "
              "precisi; se il problema si ripete, avvisi il suo amministratore.",
        "en": "I was not able to fully process your request. Please rephrase or clarify it; "
              "if this happens again, notify your administrator.",
    },
    "no_reply": {
        "fr": NO_REPLY_REPLY,
        "de": "Ich konnte diese Nachricht nicht vollständig bearbeiten. Formulieren Sie sie "
              "bitte um oder präzisieren Sie sie; falls dies erneut vorkommt, informieren "
              "Sie Ihren Administrator.",
        "it": "Non ho potuto elaborare completamente questo messaggio. Lo riformuli o lo "
              "precisi; se il problema si ripete, avvisi il suo amministratore.",
        "en": "I was not able to fully process this message. Please rephrase or clarify it; "
              "if this happens again, notify your administrator.",
    },
    "steered": {
        "fr": "J'ai pris votre message en compte dans la demande en cours.",
        "de": "Ich habe Ihre Nachricht bei der laufenden Anfrage berücksichtigt.",
        "it": "Ho tenuto conto del suo messaggio nella richiesta in corso.",
        "en": "I have taken your message into account in the current request.",
    },
    "redirected": {
        "fr": "J'ai pris votre correction en compte et j'ajuste la demande en cours.",
        "de": "Ich habe Ihre Korrektur berücksichtigt und passe die laufende Anfrage an.",
        "it": "Ho tenuto conto della sua correzione e sto adattando la richiesta in corso.",
        "en": "I have taken your correction into account and I am adjusting the current request.",
    },
    "queued": {
        "fr": "Je termine la demande en cours ; je réponds à votre message juste après.",
        "de": "Ich schliesse die laufende Anfrage ab; danach antworte ich auf Ihre Nachricht.",
        "it": "Sto terminando la richiesta in corso; rispondo al suo messaggio subito dopo.",
        "en": "I am finishing the current request; I will reply to your message right after.",
    },
    "interrupting": {
        "fr": "J'arrête la demande en cours pour répondre à votre message.",
        "de": "Ich unterbreche die laufende Anfrage, um auf Ihre Nachricht zu antworten.",
        "it": "Interrompo la richiesta in corso per rispondere al suo messaggio.",
        "en": "I am interrupting the current request to reply to your message.",
    },
}


def reply(key: str, lang=None) -> str:
    """The customer sentence for ``key``, in ``lang``; French when it is unknown."""
    par_langue = REPLIES[key]
    code = str(lang or "").strip().lower().replace("_", "-").split("-")[0]
    return par_langue.get(code) or par_langue["fr"]
# //// END Neoffice ////


# //// Neoffice — a kanban task id is opaque to the customer and useful to nobody
# outside the machinery ("La tâche `t_51f94c26` est déjà en statut…"). Stripped
# rather than rewritten: see the note in strip_internal_mechanics below (#476).
_TASK_ID_RE = re.compile(r"`?\bt_[0-9a-f]{6,}\b`?", re.IGNORECASE)


def strip_internal_mechanics(text: str, lang=None) -> str:
    """Return ``text`` with NORA's internal vocabulary removed; non-strings pass through.

    ``lang`` is the CUSTOMER's language (fr/de/it/en). It only selects which
    wording the whole-text replacements below use; every stripping rule is
    language-independent, so the delivered chat and the persisted transcript
    strip exactly the same machinery whatever is passed here.
    """
    if not text or not isinstance(text, str):
        return text
    # //// Neoffice — see _PROVIDER_FAILURE_RE above (#450).
    if _PROVIDER_FAILURE_RE.search(text[:400]):
        logger.warning("neoffice_branding: provider failure hidden from the customer: %s",
                       text[:200].replace("\n", " "))
        return reply("provider_unavailable", lang)
    # //// END Neoffice ////
    # //// Neoffice — see _GUARDRAIL_HALT_RE above (#476). Replaced WHOLE, like a
    # //// provider failure: the sentence is machinery end to end, so stripping words
    # //// out of it would leave a mangled half-sentence in the customer's chat.
    if _GUARDRAIL_HALT_RE.search(text[:400]):
        logger.warning("neoffice_branding: tool-call guardrail hidden from the customer: %s",
                       text[:200].replace("\n", " "))
        return reply("guardrail_halt", lang)
    # //// END Neoffice ////
    # //// Neoffice — upstream turn notices, see _BUSY_NOTICES above (#491 thread).
    # //// Replaced WHOLE: head, status detail and tail are machinery end to end, so
    # //// stripping words would leave a mangled half-sentence in the customer's chat.
    for _pattern, _key in _BUSY_NOTICES:
        if _pattern.search(text[:120]):
            logger.info("neoffice_branding: turn notice rewritten for the customer: %s",
                        text[:200].replace("\n", " "))
            return reply(_key, lang)
    if _NO_REPLY_RE.search(text[:120]):
        logger.warning("neoffice_branding: unanswered turn hidden from the customer: %s",
                       text[:200].replace("\n", " "))
        return reply("no_reply", lang)
    # //// END Neoffice ////
    # //// Neoffice — upstream's failure copy, see _UPSTREAM_FAILURE_COPY_RE above.
    if _UPSTREAM_FAILURE_COPY_RE.search(text):
        _key = "provider_unavailable" if _PROVIDER_FAILURE_COPY_RE.search(text) else "no_reply"
        logger.warning("neoffice_branding: upstream failure copy hidden from the customer (%s): %s",
                       _key, text[:200].replace("\n", " "))
        return reply(_key, lang)
    # //// END Neoffice ////
    # //// Neoffice — capitalised when it opens a sentence (20.09). The replacement
    # //// was the bare « l'équipe », so « Le spécialiste compta va… » came out
    # //// « l'équipe va… » — a sentence opening on a lowercase letter, on every pole,
    # //// for as long as this rule has existed. What is replaced here is a NOUN
    # //// PHRASE, and a noun phrase carries the case of the position it lands in.
    # //// `source` is bound as a default so the closure reads the text being scanned,
    # //// not whatever `text` is rebound to afterwards.
    def _team(match, source=text):
        head = source[: match.start()]
        opens = (
            not head.strip()
            or head.rstrip(" \t").endswith("\n")
            # //// Neoffice — no colon here: French does NOT capitalise after « : »
            # //// (« Je transmets : l'équipe vous rappellera »). A bullet does open a
            # //// phrase, so it stays.
            or head.rstrip()[-1] in ".!?•-*"
        )
        return "L'équipe" if opens else "l'équipe"

    text = _ROLE_RE.sub(_team, text)
    text = _SPECIALIST_RE.sub("équipe", text)
    # //// Neoffice — « demande », not « ta tâche » (20.09). The machinery says
    # //// « Votre tâche kanban est terminée » and the replacement turned it into
    # //// « Votre ta tâche est terminée » — a determiner already stands in front of
    # //// the noun, so the noun alone goes in. And this product vouvoies: « ta »
    # //// addressed the customer as tu, which nothing else here does.
    # //// Feminine like « tâche », so « votre / la / une » all still agree.
    text = _KANBAN_TASK_RE.sub("demande", text)
    # //// Neoffice — the id goes, the sentence stays (#476). A state sentence
    # //// ("… est déjà en statut `done`") cannot be safely REWRITTEN this late: we
    # //// would be telling a customer something about their own work from a regex,
    # //// and a wrong state is worse than an awkward one. So the opaque id is
    # //// removed here and the sentence itself is fixed where it is WRITTEN, in the
    # //// kanban tool, not rescued at the door.
    text = _TASK_ID_RE.sub("", text)
    # //// END Neoffice ////
    text = _INTERNALS_RE.sub("", text)
    return _SPACES_RE.sub(" ", text).strip()
