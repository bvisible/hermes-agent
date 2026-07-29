"""Neoffice — guard the mem0 conversational-filler filter.

Per-turn capture runs with ``infer=False``, so nothing judges what lands in the
store: every "Bonjour" and "C'est bien noté" became a permanent memory (25% of a
real user's store, measured on osiris 2026-07-29). That is not only clutter —
the recalled block is injected into the system prompt, so a store that grows
every turn changes the prompt prefix every turn and defeats llama.cpp's prefix
cache (~22k tokens re-prefilled, 7-8s, instead of ~0.2s).

The contract these tests lock down is asymmetric ON PURPOSE: memory is the
product's core value, so dropping a real fact is a regression, while keeping an
extra ack is merely untidy. Hence: when in doubt, remember.
"""
import re
from pathlib import Path
from typing import Optional

import pytest

_PLUGIN = Path(__file__).resolve().parents[2] / "plugins" / "memory" / "mem0" / "__init__.py"


def _load_filter():
    """Exec just the filter block — importing the plugin needs the mem0 SDK."""
    src = _PLUGIN.read_text(encoding="utf-8")
    start = src.index("_ACK_OPENING_RE = re.compile(")
    end = src.index("# //// END Neoffice ////", start)
    ns: dict = {"re": re, "Optional": Optional}
    exec(compile(src[start:end], str(_PLUGIN), "exec"), ns)  # noqa: S102
    return ns["_is_low_value_for_memory"]


is_low_value = _load_filter()


# Real strings observed in a production store — these carry information.
@pytest.mark.parametrize(
    "text",
    [
        "Retiens ceci : mon code de coffre est QW771234.",
        "Le code de votre projet Genève est ZK286492.",
        "The user's Geneva project code is ZK966442.",
        # An ack that DOES carry a value: the digit rule must win over the opening.
        "C'est bien noté, j'ai mis à jour le code du projet Genève avec la valeur ZK728402.",
        # Digit-free facts: kept because they do not open with a politeness formula.
        "L'utilisateur préfère être vouvoyé et travaille depuis Genève.",
        "Mon comptable s'appelle Marc Dupont.",
        "La société n'est pas assujettie à la TVA.",
        # Long content is substance by definition.
        "Je n'ai pas encore reçu de réponse de la part du pôle Comptabilité, "
        "mais je reste à votre écoute pour toute autre demande.",
    ],
)
def test_facts_are_never_dropped(text):
    assert is_low_value(text) is False, f"a fact would be lost: {text!r}"


@pytest.mark.parametrize(
    "text",
    [
        "Bonjour",
        "Bonsoir",
        "Merci",
        "D'accord",
        "Parfait",
        "C'est bien noté, j'ai enregistré votre code de coffre.",
        "Je vais très bien, merci de demander ! Comment puis-je vous aider aujourd'hui ?",
        "Noted",
        "",
        "   ",
        None,
    ],
)
def test_pure_filler_is_dropped(text):
    assert is_low_value(text) is True, f"filler would be stored: {text!r}"


def test_unknown_shapes_default_to_remembering():
    """Anything the pattern does not recognise must be kept (default = remember)."""
    assert is_low_value("Le chantier démarre lundi prochain sans faute.") is False
    assert is_low_value("Rappelle-moi de relancer le fournisseur.") is False
