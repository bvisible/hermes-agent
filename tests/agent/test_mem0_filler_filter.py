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
    # End at the first TOP-LEVEL statement after the function, not at a marker.
    # Marker-based slicing broke the moment a //// Neoffice //// block was added
    # INSIDE the function: the slice stopped at that inner marker, the trailing
    # returns were cut off, and the function silently returned None — every
    # assertion then failed for a reason that had nothing to do with the filter.
    fn = src.index("def _is_low_value_for_memory")
    after = src.index("\n", src.index("return bool(_ACK_OPENING_RE.match(stripped))", fn))
    end = after
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


# Recall questions state nothing — the user is querying, not informing. Four
# copies of the same question were found in a production store.
@pytest.mark.parametrize(
    "text",
    [
        "Tu te souviens du code de mon projet Genève que je t'ai donné tout à l'heure ?",
        "Te rappelles-tu de mon adresse ?",
        "Quel est mon code de chantier ?",
        "C'est quoi mon code déjà ?",
        "Peux-tu me rappeler le nom de mon comptable ?",
        "Do you remember my address?",
        "What is my project code?",
    ],
)
def test_recall_questions_are_not_memorised(text):
    assert is_low_value(text) is True, f"recall question would be stored: {text!r}"


@pytest.mark.parametrize(
    "text",
    [
        # Questions that CARRY information must survive — the user is informing.
        "Peux-tu noter que je préfère le vouvoiement ?",
        "Tu peux enregistrer que mon comptable est Marc Dupont ?",
        # A recall question that still states a value keeps it (digit rule wins).
        "Tu te souviens que mon code est RT445566 ?",
    ],
)
def test_questions_carrying_information_are_kept(text):
    assert is_low_value(text) is False, f"a fact would be lost: {text!r}"


# //// Neoffice — bare acknowledgements and sign-offs (NORA #43).
# Measured on the osiris store: 1424 of 1425 points were raw conversational
# capture, and searching a supplier name returned "Oui." and "À tout à l'heure !"
# scoring ABOVE the real facts.
@pytest.mark.parametrize(
    "text",
    [
        "Oui.", "Non", "Ouais", "Voilà", "Exact", "C'est ça", "Super", "Tout à fait",
        "À tout à l'heure !", "Bonne journée", "Au revoir", "Bye",
        "Comment je m'appelle ?", "Qui suis-je ?",
    ],
)
def test_bare_acknowledgements_are_not_memorised(text):
    assert is_low_value(text) is True, f"noise would be stored: {text!r}"


@pytest.mark.parametrize(
    "text",
    [
        # An acknowledgement that INTRODUCES a fact must survive whole — dropping
        # these was the regression an opening-prefix rule caused in testing.
        "Oui, le fournisseur Sateldranse livre le ciment",
        "Non, c'est Romande Énergie notre fournisseur d'électricité",
        "Exact, et il facture aussi la fibre",
        "Voilà pourquoi on impute Sunrise en 6510",
    ],
)
def test_acknowledgement_introducing_a_fact_is_kept(text):
    assert is_low_value(text) is False, f"a fact would be lost: {text!r}"
