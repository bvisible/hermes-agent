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
# Drop this module if the assistant is ever taught not to name its own internals.
"""

from __future__ import annotations

import re

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


def strip_internal_mechanics(text: str) -> str:
    """Return ``text`` with NORA's internal vocabulary removed; non-strings pass through."""
    if not text or not isinstance(text, str):
        return text
    text = _ROLE_RE.sub("l'équipe", text)
    text = _SPECIALIST_RE.sub("équipe", text)
    text = _KANBAN_TASK_RE.sub("ta tâche", text)
    text = _INTERNALS_RE.sub("", text)
    return _SPACES_RE.sub(" ", text).strip()
