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
    r"(compta\w*|ventes?|support|rh|ressources?\s+humaines?|commercial\w*)\b[`'\"*]*",
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
_GUARDRAIL_HALT_RE = re.compile(r"\bhit the tool-call guardrail\b", re.IGNORECASE)
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
_BUSY_NOTICES = (
    (re.compile(r"^\s*⇩?⏩?\s*Steered into current run\b", re.IGNORECASE),
     "J'ai pris votre message en compte dans la demande en cours."),
    (re.compile(r"^\s*↪?\s*Redirected current run\b", re.IGNORECASE),
     "J'ai pris votre correction en compte et j'ajuste la demande en cours."),
    (re.compile(r"^\s*⏳?\s*(Subagent working|Compressing context|Queued for the next turn)\b",
                re.IGNORECASE),
     "Je termine la demande en cours ; je réponds à votre message juste après."),
    (re.compile(r"^\s*⚡?\s*Interrupting current task\b", re.IGNORECASE),
     "J'arrête la demande en cours pour répondre à votre message."),
)

_NO_REPLY_RE = re.compile(r"^\s*⚠️?\s*No reply\s*:", re.IGNORECASE)
NO_REPLY_REPLY = (
    "Je n'ai pas réussi à aller au bout de ce message. "
    "Reformulez-le ou précisez-le ; si cela se reproduit, prévenez votre administrateur."
)


# //// Neoffice — a kanban task id is opaque to the customer and useful to nobody
# outside the machinery ("La tâche `t_51f94c26` est déjà en statut…"). Stripped
# rather than rewritten: see the note in strip_internal_mechanics below (#476).
_TASK_ID_RE = re.compile(r"`?\bt_[0-9a-f]{6,}\b`?", re.IGNORECASE)


def strip_internal_mechanics(text: str) -> str:
    """Return ``text`` with NORA's internal vocabulary removed; non-strings pass through."""
    if not text or not isinstance(text, str):
        return text
    # //// Neoffice — see _PROVIDER_FAILURE_RE above (#450).
    if _PROVIDER_FAILURE_RE.search(text[:400]):
        logger.warning("neoffice_branding: provider failure hidden from the customer: %s",
                       text[:200].replace("\n", " "))
        return PROVIDER_UNAVAILABLE_REPLY
    # //// END Neoffice ////
    # //// Neoffice — see _GUARDRAIL_HALT_RE above (#476). Replaced WHOLE, like a
    # //// provider failure: the sentence is machinery end to end, so stripping words
    # //// out of it would leave a mangled half-sentence in the customer's chat.
    if _GUARDRAIL_HALT_RE.search(text[:400]):
        logger.warning("neoffice_branding: tool-call guardrail hidden from the customer: %s",
                       text[:200].replace("\n", " "))
        return GUARDRAIL_HALT_REPLY
    # //// END Neoffice ////
    # //// Neoffice — upstream turn notices, see _BUSY_NOTICES above (#491 thread).
    # //// Replaced WHOLE: head, status detail and tail are machinery end to end, so
    # //// stripping words would leave a mangled half-sentence in the customer's chat.
    for _pattern, _french in _BUSY_NOTICES:
        if _pattern.search(text[:120]):
            logger.info("neoffice_branding: turn notice rewritten for the customer: %s",
                        text[:200].replace("\n", " "))
            return _french
    if _NO_REPLY_RE.search(text[:120]):
        logger.warning("neoffice_branding: unanswered turn hidden from the customer: %s",
                       text[:200].replace("\n", " "))
        return NO_REPLY_REPLY
    # //// END Neoffice ////
    text = _ROLE_RE.sub("l'équipe", text)
    text = _SPECIALIST_RE.sub("équipe", text)
    text = _KANBAN_TASK_RE.sub("ta tâche", text)
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
