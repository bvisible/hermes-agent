# //// Neoffice — added file (no upstream equivalent): « à quatre pièces » is a quantity.
"""Capability bench, 2026-09-24 23:49. « Sur le devis DEVIS-2026-01190, passe les mitigeurs à
quatre pièces » reached ventes; the worker had the quotation's line in front of it, read
« mitigeur 4 pièces » as an article's name, searched for it until the loop guard, and answered
that it could not. The router now says, in code, that the count is a quantity."""
import sys
import types
from types import SimpleNamespace

import pytest

from gateway import nora_chat_router as router


@pytest.mark.parametrize("message, n", (
    ("Sur le devis DEVIS-2026-01190, passe les mitigeurs à quatre pièces.", 4),
    ("Mets 3 pièces de siphon au lieu de 2 sur la facture FA-2026-00012", 3),
    ("Passe la quantité des mitigeurs à 4 sur ce devis", "4"),
    ("Augmente les mitigeurs à dix unités", 10),
    ("Setz die Mischbatterien auf vier Stück", 4),
    ("Porta i miscelatori a quattro pezzi", 4),
    ("Change the mixers to four pieces on the quote", 4),
    ("Set the quantity to 12 on line 2", "12"),
))
def test_a_count_with_its_unit_in_a_change_is_a_quantity(message, n):
    hint = router.quantity_change_hint(message)
    assert hint and f"QUANTITY ({n})" in hint, hint
    assert "never search for an article whose name contains the number" in hint


@pytest.mark.parametrize("message", (
    "Mets le prix du EAP723 à 95 CHF",                    # a price
    "Fais une remise de 5 % sur tout le devis DEVIS-2026-01190",
    "Commande 4 pièces de mitigeur chez le fournisseur",  # no change: an order to create
    "Crée un devis pour 4 pièces de mitigeur",
    "Passe la facture FA-2026-00012 en payée",            # a change, no count of articles
    "Change la quantité d'une ligne",                     # no count given
    "Rappelle-moi dans 2 jours de relancer ce client",
))
def test_no_hint_without_a_count_of_articles_being_changed(message):
    assert router.quantity_change_hint(message) is None


def _fake_kanban(monkeypatch):
    created = []
    kanban_db = types.SimpleNamespace(create_task=lambda conn, **kw: created.append(kw) or "t_test")
    connect = types.SimpleNamespace(connect=lambda board=None: SimpleNamespace(close=lambda: None))
    notify = types.SimpleNamespace(add_notify_sub=lambda *a, **kw: None)
    pkg = sys.modules.get("hermes_cli") or types.ModuleType("hermes_cli")
    monkeypatch.setitem(sys.modules, "hermes_cli", pkg)
    for name, mod in (("kanban_db", kanban_db), ("kanban_db_connect", connect), ("kanban_db_notify", notify)):
        monkeypatch.setattr(pkg, name, mod, raising=False)
        monkeypatch.setitem(sys.modules, f"hermes_cli.{name}", mod)
    return created


def test_the_hint_reaches_the_ventes_worker(monkeypatch):
    created = _fake_kanban(monkeypatch)
    message = "Sur le devis DEVIS-2026-01190, passe les mitigeurs à quatre pièces."
    router.route_chat_message(
        message=message, session_chat_id="webhook:nora_chat:t", conversation_id="conv-qty-1",
        thread_id=None, user_id="u", notifier_profile="default", idempotency_key="k-qty-1",
        call_llm_fn=lambda **_kw: SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ventes"))]),
        main_runtime=None, page_context=None)
    assert created, "the request did not reach a pole"
    assert created[-1]["assignee"] == "ventes"
    assert "« quatre pièces » is a QUANTITY (4)" in created[-1]["body"]
    assert message in created[-1]["body"]
