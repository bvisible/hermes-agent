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
    text = _ROLE_RE.sub("l'équipe", text)
    text = _SPECIALIST_RE.sub("équipe", text)
    text = _KANBAN_TASK_RE.sub("ta tâche", text)
    text = _INTERNALS_RE.sub("", text)
    return _SPACES_RE.sub(" ", text).strip()
