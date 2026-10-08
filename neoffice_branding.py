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
    # //// Neoffice — « des ventes », « du support », « de l'analyse » too (07.10): only « de (la) »
    # //// was read, so « le spécialiste des ventes » escaped this rule.
    r"[\s`'\"*_:.\-]*(?:(?:de|des|du)\s+(?:la\s+|l['’]\s*)?|d['’]\s*)?[\s`'\"*_:.\-]*"
    # //// Neoffice — `analyse` and `projet` added (20.09). Without them the phrase
    # //// escapes THIS rule and meets _SPECIALIST_RE below, which replaces the bare
    # //// word: « Le spécialiste projet va… » came out « Le équipe projet va… ».
    # //// A list of poles written by hand drifts the day a pole is added — these two
    # //// arrived on 16.09 and nothing here knew.
    r"(compta\w*|ventes?|support|rh|ressources?\s+humaines?|commercial\w*"
    r"|analyses?|projets?)\b[`'\"*]*",
    re.IGNORECASE,
)
# //// Neoffice — « spécialiste » is replaced only where it stands for the machinery (07.10, #1294).
# //// This rule replaced every occurrence: a candidate's « brevet fédéral de spécialiste en finance
# //// et comptabilité » reached the HR desk as « brevet fédéral de équipe en finance ». A specialist
# //// OF something (en, de, du, des, d', RH, IT, avec brevet) is a profession or a diploma and stays
# //// as written. The machinery's word comes with its determiner, which is replaced with it:
# //// « le spécialiste va traiter… » came out « le équipe va traiter… ».
_SPECIALIST_RE = re.compile(
    r"\b(?:(le|la|un|une|du|au|notre|votre|ce|cette|les|des|aux|nos|vos|ces)\s+)?sp[ée]cialistes?\b"
    r"(?!\s+(?:en|de|des|du|d['’]|rh|it|avec)\b)",
    re.IGNORECASE,
)
_SPECIALIST_TEAM = {
    "du": "de l'équipe", "des": "de l'équipe", "au": "à l'équipe", "aux": "à l'équipe",
    "notre": "notre équipe", "nos": "notre équipe", "votre": "votre équipe", "vos": "votre équipe",
    "ce": "cette équipe", "cette": "cette équipe", "ces": "cette équipe",
}
# //// END Neoffice ////
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
# //// v0.21.6 translates the gateway's copy (agent/i18n.py), so a sentence anchored on its
# //// English words misses the same copy in French: the commands are the part no catalog
# //// translates — `continue` and any backticked `hermes …` command in every language, plus
# //// /reset, /login and /stop, which the gateway's error copy names.
_UPSTREAM_FAILURE_COPY_RE = re.compile(
    r"(?:^|[\s(`])/(?:retry|model|new|compress|reasoning|reset|login|stop)\b"
    r"|`hermes\b"
    r"|`continue`"
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
    # //// Neoffice — two meanings the gateway's own notices carry (v0.21.6, see _CATALOG_NOTICES):
    # //// the message is KEPT and will be answered after a restart, or it was NOT taken and must
    # //// be sent again. Different instructions for the reader, so different sentences.
    "restarting": {
        "fr": "Je redémarre un instant ; votre message sera traité dès mon retour.",
        "de": "Ich starte kurz neu; Ihre Nachricht wird bearbeitet, sobald ich wieder da bin.",
        "it": "Mi sto riavviando un istante; il suo messaggio sarà elaborato appena sarò di nuovo disponibile.",
        "en": "I am restarting for a moment; your message will be handled as soon as I am back.",
    },
    "resend": {
        "fr": "Je n'ai pas pu traiter votre message pour le moment. Renvoyez-le dans un instant.",
        "de": "Ich konnte Ihre Nachricht im Moment nicht bearbeiten. Senden Sie sie bitte in einem "
              "Augenblick erneut.",
        "it": "Non ho potuto elaborare il suo messaggio in questo momento. Lo invii di nuovo tra un istante.",
        "en": "I could not handle your message just now. Please send it again in a moment.",
    },
}


def reply(key: str, lang=None) -> str:
    """The customer sentence for ``key``, in ``lang``; French when it is unknown."""
    by_language = REPLIES[key]
    code = str(lang or "").strip().lower().replace("_", "-").split("-")[0]
    return by_language.get(code) or by_language["fr"]
# //// END Neoffice ////


# //// Neoffice — upstream's gateway notices are CATALOGUED since v0.21.6 (agent/i18n.py) and
# //// rendered in the profile's display.language, which provision.sh sets to French so the
# //// clarify prompt reads French (our own French literal until then). Every rule above
# //// anchors on English words: in French, « ↪ Exécution en cours redirigée », « ⚠️ Pas de
# //// réponse : … » or « ⏳ Le gateway est en cours de redémarrage… » reached the customer
# //// whole (measured on the merged tree, 08.10: seven notices out of eight).
# ////
# //// So these notices are also recognised by their catalog entry, read from the catalog the
# //// process renders with: the active language, and English, which t() falls back to for a
# //// key the language lacks. Whatever display.language says, the rule holds, and a reworded
# //// entry is still the same entry. Each key's text OPENS the message the gateway sends (the
# //// head of a busy notice, the prefix of an unanswered turn, the whole of an error); the
# //// anchor is its fixed part up to the first placeholder, leading symbol REQUIRED, so an
# //// answer that merely opens on the same words (« Pas de réponse du client depuis lundi »)
# //// is left alone. The test renders every key with upstream's own code, in four languages.
_CATALOG_NOTICES = (
    ("gateway.busy.steered_subagents_head", "steered"),
    ("gateway.busy.steered_head", "steered"),
    ("gateway.busy.redirected_head", "redirected"),
    ("gateway.busy.subagent_working_head", "queued"),
    ("gateway.busy.compressing_head", "queued"),
    ("gateway.busy.queued_head", "queued"),
    ("gateway.busy.interrupting_head", "interrupting"),
    ("gateway.busy.drain_queued", "restarting"),
    ("gateway.busy.drain_rejected", "resend"),
    ("gateway.busy.drain_rejected_new_work", "resend"),
    ("gateway.busy.draining_maintenance", "resend"),
    ("gateway.busy.another_turn_running", "resend"),
    ("explainer.no_reply_prefix", "no_reply"),
    ("gateway.errors.rate_limited", "provider_unavailable"),
    ("gateway.errors.auth_failed", "provider_unavailable"),
    ("gateway.errors.connection_interrupted", "provider_unavailable"),
    ("gateway.errors.unreachable", "provider_unavailable"),
    ("gateway.errors.connection_unknown", "provider_unavailable"),
    ("gateway.errors.usage_limit_resets", "provider_unavailable"),
    ("gateway.errors.provider_kept_failing", "provider_unavailable"),
    ("gateway.errors.no_credentials", "provider_unavailable"),
    ("gateway.errors.bad_request", "no_reply"),
    ("gateway.errors.context_overflow", "no_reply"),
    ("gateway.errors.generic_failed", "no_reply"),
    ("gateway.errors.generic_failed_with_hint", "no_reply"),
    ("gateway.errors.stopped_before_finishing", "no_reply"),
    ("gateway.errors.no_response", "no_reply"),
    ("gateway.errors.unexpected_silence", "no_reply"),
    ("gateway.errors.history_unavailable", "no_reply"),
    ("gateway.errors.interrupted_before_start", "resend"),
    ("gateway.errors.previous_turn_cleanup", "resend"),
    ("gateway.errors.session_storage_unavailable", "resend"),
    ("gateway.errors.session_storage_unavailable_disk", "resend"),
)
# The notices a customer causes by typing while NORA works: ordinary impatience, logged at INFO.
_IMPATIENCE_REPLIES = frozenset({"steered", "redirected", "queued", "interrupting", "restarting"})
_CATALOG_ANCHOR_WORDS = 8
_PLACEHOLDER_RE = re.compile(r"\{[^{}]*\}")
_catalog_anchors_by_language: dict = {}


def _catalog_anchor(text: str):
    """The start-anchored pattern of a catalog entry's opening; None when too thin to be specific.

    The opening runs over placeholders (a bounded wildcard each) up to eight literal words: two
    entries can share everything before their first placeholder — « ⏳ Gateway {action} — queued… »
    and « ⏳ Gateway {action} und nimmt… » — and differ only after it (found by the test, 08.10).
    """
    template = (text or "").strip()
    cut = 0
    while cut < len(template) and not template[cut].isalnum() and template[cut] != "{":
        cut += 1
    symbols = [c for c in template[:cut] if not c.isspace() and c != "\ufe0f"]
    parts, words_left, literal = [], _CATALOG_ANCHOR_WORDS, ""
    for index, chunk in enumerate(_PLACEHOLDER_RE.split(template[cut:])):
        if index:
            parts.append(r".{0,120}?")
        words = chunk.split()[:words_left]
        words_left -= len(words)
        literal += "".join(words)
        if words:
            parts.append(r"\s+".join(re.escape(word) for word in words))
        if words_left <= 0:
            break
    while parts and parts[-1] == r".{0,120}?":
        parts.pop()
    if len(literal) < 5 or (not symbols and _CATALOG_ANCHOR_WORDS - words_left < 3):
        return None
    lead = "".join(re.escape(c) + "\ufe0f?" for c in symbols)
    return re.compile(r"\s*" + lead + r"\s*" + r"\s*".join(parts), re.IGNORECASE)


def _catalog_notices() -> tuple:
    """(pattern, reply key) for every catalogued notice, in the active language and in English.

    Built once per language and cached: the catalogs are the ones t() already loaded to render
    the notices, so the cost is a few dozen regexes. Empty when the catalog cannot be read; the
    English anchors above stay as the floor.
    """
    try:
        from agent.i18n import get_language, t

        active = get_language()
    except Exception:
        return ()
    cached = _catalog_anchors_by_language.get(active)
    if cached is not None:
        return cached
    anchors = []
    for language in dict.fromkeys((active, "en")):
        for key, reply_key in _CATALOG_NOTICES:
            try:
                text = t(key, lang=language)
            except Exception:
                continue
            pattern = _catalog_anchor(text) if text and text != key else None
            if pattern is not None:
                anchors.append((pattern, reply_key))
    _catalog_anchors_by_language[active] = tuple(anchors)
    return _catalog_anchors_by_language[active]
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
    # //// Neoffice — the same notices in the catalog's words, any language (v0.21.6), see
    # //// _CATALOG_NOTICES above.
    for _pattern, _key in _catalog_notices():
        if _pattern.match(text):
            logger.log(logging.INFO if _key in _IMPATIENCE_REPLIES else logging.WARNING,
                       "neoffice_branding: gateway notice (%s) rewritten for the customer: %s",
                       _key, text[:200].replace("\n", " "))
            return reply(_key, lang)
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

    # //// Neoffice — see _SPECIALIST_RE above (07.10): the determiner goes with the word, and the
    # //// phrase takes the case of where it lands, like _team.
    def _specialist(match, source=text):
        phrase = _SPECIALIST_TEAM.get((match.group(1) or "").lower(), "l'équipe")
        head = source[: match.start()]
        opens = not head.strip() or head.rstrip(" \t").endswith("\n") or head.rstrip()[-1] in ".!?•-*"
        return phrase[0].upper() + phrase[1:] if opens else phrase

    text = _SPECIALIST_RE.sub(_specialist, text)
    # //// END Neoffice ////
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
