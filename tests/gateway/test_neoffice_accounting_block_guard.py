"""Neoffice regression tests for safe accounting clarification delivery."""

import runpy
from pathlib import Path


# Load the mixin module directly: importing ``gateway`` eagerly loads every
# platform dependency, while this guard is deliberately a pure helper.
_WATCHERS = Path(__file__).resolve().parents[2] / "gateway" / "kanban_watchers.py"
_safe_accounting_block_reason = runpy.run_path(str(_WATCHERS))[
    "_safe_accounting_block_reason"
]


def _guard(reason, *, title="Imputation comptable", body="Dans quel compte imputer la facture ?"):
    return _safe_accounting_block_reason(
        reason,
        assignee="compta",
        title=title,
        body=body,
    )


def test_keeps_only_safe_question_from_speculative_account_reason():
    reason = (
        "Deux traitements sont possibles.\n"
        "1. Au-dessus du seuil, utiliser le compte 1520.\n"
        "2. Au-dessous, utiliser le compte 6570.\n"
        "Quel est le montant de l'achat et quel seuil d'activation appliquez-vous ?"
    )

    delivered = _guard(reason)

    assert delivered == (
        "Quel est le montant de l'achat et quel seuil d'activation appliquez-vous ?"
    )
    assert "1520" not in delivered
    assert "6570" not in delivered


def test_uses_neutral_fallback_when_question_itself_contains_account_numbers():
    delivered = _guard("Faut-il utiliser le compte 1520 ou le compte 6570 ?")

    assert delivered == (
        "Quelle information factuelle manque-t-il pour départager les traitements "
        "comptables possibles ?"
    )
    assert "1520" not in delivered
    assert "6570" not in delivered


def test_preserves_safe_accounting_question():
    question = "Quel est le montant hors TVA et la politique d'activation interne ?"

    assert _guard(question) == question


def test_asks_the_decisive_activation_facts_when_worker_only_explains_options():
    delivered = _guard(
        "Selon le montant et la politique d'activation, le compte 1520 ou 6570 "
        "pourrait être envisagé.",
        body="Dans quel compte imputer cet ordinateur durable ?",
    )

    assert delivered == (
        "Quel est le montant de l'achat et quelle politique ou quel seuil "
        "d'activation votre entreprise applique-t-elle à ce type de matériel ?"
    )
    assert "1520" not in delivered
    assert "6570" not in delivered


def test_does_not_filter_other_domains_or_non_allocation_accounting_tasks():
    legal_reason = "L'article 957a CO doit être confirmé. Quelle période viser ?"

    assert _safe_accounting_block_reason(
        legal_reason,
        assignee="compta",
        title="Durée de conservation",
        body="Confirmer la source légale.",
    ) == legal_reason
    assert _safe_accounting_block_reason(
        "Ticket 1520 : quel choix ?",
        assignee="support",
        title="Incident",
        body="Quel traitement appliquer ?",
    ) == "Ticket 1520 : quel choix ?"
