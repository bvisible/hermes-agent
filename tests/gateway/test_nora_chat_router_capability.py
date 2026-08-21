"""Capability-question gate regressions (WI voice console, 2026-08-21).

//// Neoffice — added file (no upstream equivalent).
The light capability path answered "Est-ce que tu peux m'en mettre quinze en
commande ?" with a generic "Oui, je peux gérer les commandes clients — quel
produit, quel client ?" while the PREVIOUS turn had just identified the product
(0 stock) and offered a purchase order. Root cause: the data guard only looked
for [0-9], and voice transcription spells figures out; and no guard covered
object pronouns (m'en → the product under discussion). An order must reach its
pole with the conversation; only cold meta-questions may take the light path.
"""

import pytest

from gateway.nora_chat_router import _CAPABILITY_RE


@pytest.mark.parametrize(
    "message",
    (
        # The live bug, verbatim (voice writes figures as words).
        "Est-ce que tu peux m'en mettre quinze en commande?",
        # Same with a digit — the historical guard already caught it; keep it pinned.
        "Est-ce que tu peux m'en commander 15 ?",
        # Spelled-out quantity without a pronoun.
        "Peux-tu préparer une commande d'achat de quinze unités ?",
        # Object pronoun without a quantity.
        "Peux-tu me le commander ?",
        "Est-ce que tu peux nous en livrer trois ?",
    ),
)
def test_orders_with_anaphora_or_spelled_quantities_are_not_capability(message):
    assert not _CAPABILITY_RE.match(message)


@pytest.mark.parametrize(
    "message",
    (
        # Genuine capability questions — "un/une" are articles, not quantities,
        # and must NOT be treated as data.
        "est-ce que tu peux créer un client ?",
        "Est-ce que tu peux créer une facture ?",
        "tu sais gérer les devis ?",
        "c'est possible d'envoyer une relance ?",
    ),
)
def test_genuine_capability_questions_still_take_the_light_path(message):
    assert _CAPABILITY_RE.match(message)
