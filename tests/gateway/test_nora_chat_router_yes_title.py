# //// Neoffice — added file (no upstream equivalent): a bare yes is titled by what it confirms (08.10).
"""Quick Chat, 08.10: NORA showed a price change, the person answered « Oui, vas-y. », and the result read
« ✅ Ventes — Oui, vas-y. »: a worker's result is titled by its task, and the task was titled by the message.
A bare yes now takes the title of the request it answers, and a second yes keeps it."""
import sys
import types
from types import SimpleNamespace

import pytest

from gateway import nora_chat_router as router

REQUEST = "Passe le prix de vente de l'arrosoir en zinc à 39 francs."


@pytest.mark.parametrize("yes", ("Oui, vas-y.", "oui", "Ok", "D'accord", "vas-y"))
def test_a_bare_yes_takes_the_title_of_what_it_confirms(yes):
    assert router._task_title(yes, {"title": REQUEST, "msg": REQUEST, "pole": "ventes"}) == REQUEST


def test_a_record_from_before_the_title_gives_its_message():
    assert router._task_title("Oui, vas-y.", {"msg": REQUEST, "pole": "ventes"}) == REQUEST


@pytest.mark.parametrize("message", (
    "Oui mais plutôt 40 francs",
    "Non",
    "Fais une remise de 5 % sur tout le devis DEVIS-2026-00042.",
))
def test_anything_else_keeps_its_own_words(message):
    assert router._task_title(message, {"title": REQUEST}) == message


def test_a_yes_with_nothing_before_is_itself():
    assert router._task_title("Oui", None) == "Oui"


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


def test_the_confirmed_task_is_titled_by_the_request(monkeypatch):
    created = _fake_kanban(monkeypatch)
    router._LAST_ROUTE.pop("conv-yes-title", None)
    router._CONV_HISTORY.pop("conv-yes-title", None)
    ventes = lambda **_kw: SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ventes"))])  # noqa: E731
    for i, message in enumerate((REQUEST, "Oui, vas-y.", "Oui")):
        router.route_chat_message(
            message=message, session_chat_id="webhook:nora_chat:t", conversation_id="conv-yes-title",
            thread_id=None, user_id="u", notifier_profile="default", idempotency_key=f"k-yes-{i}",
            call_llm_fn=ventes, main_runtime=None, page_context=None)
    assert [c["assignee"] for c in created] == ["ventes", "ventes", "ventes"]
    assert [c["title"] for c in created] == [REQUEST, REQUEST, REQUEST]
