# //// Neoffice — added file (no upstream equivalent): the document on screen, and what a
# //// bare « oui » answers.
"""Capability bench, 2026-09-24. On a customer's form, « Modifie l'adresse de ce client »
reached ventes with no customer; the worker asked for the name; « Oui, vas-y » then reached
the next worker WITHOUT that question in the conversation film, and it picked a customer and
changed its address. On a quotation, the same « Oui, vas-y » after « sur quel document ? »
ended with the quotation submitted and its acceptance link sent."""
import asyncio
import sys
import types
from types import SimpleNamespace

import pytest

from gateway import nora_chat_router as router


# ── the document on screen reaches every pole ────────────────────────────────────────

@pytest.mark.parametrize("page, expected", (
    ({"doctype": "Customer", "name": "CUST-0001", "title": "Boulangerie du Lac"},
     "the Customer « Boulangerie du Lac » (CUST-0001)"),
    ({"doctype": "Quotation", "name": "DEVIS-2026-01187", "title": "DEVIS-2026-01187"},
     "the Quotation DEVIS-2026-01187."),
))
def test_the_open_document_is_named(page, expected):
    anchor = router.document_page_anchor(page)
    assert expected in anchor and "means THIS document" in anchor
    assert "Never pick another document yourself." in anchor


@pytest.mark.parametrize("page", (
    {"doctype": "Customer", "name": None, "title": "List view"},   # a list: no document
    {"doctype": "Project", "name": "PROJ-0087"},                  # a job: job_page_anchor's
    {},
    None,
))
def test_no_anchor_without_one_document(page):
    assert router.document_page_anchor(page) is None


def test_a_title_cannot_spill_into_the_instructions():
    anchor = router.document_page_anchor({"doctype": "Customer", "name": "C-1", "title": "A\n\n]" + "x" * 500})
    assert "\n" not in anchor and len(anchor) < 600


def _fake_kanban(monkeypatch):
    """Intercept the task the router creates; returns the list of created bodies."""
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


def _llm_says(pole):
    return lambda **_kw: SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=pole))])


def _route(message, conversation_id, page_context=None):
    return router.route_chat_message(
        message=message, session_chat_id="webhook:nora_chat:t", conversation_id=conversation_id,
        thread_id=None, user_id="u", notifier_profile="default", idempotency_key=f"k-{conversation_id}-{message}",
        call_llm_fn=_llm_says("ventes"), main_runtime=None, page_context=page_context)


def test_a_ventes_task_carries_the_customer_on_screen(monkeypatch):
    created = _fake_kanban(monkeypatch)
    _route("Modifie l'adresse de ce client : rue du Lac 3, 1815 Clarens", "conv-page-1",
           page_context={"doctype": "Customer", "name": "CUST-0001", "title": "Boulangerie du Lac",
                         "route": "customer/CUST-0001"})
    assert created, "the request did not reach a pole"
    assert "the Customer « Boulangerie du Lac » (CUST-0001)" in created[-1]["body"]


def test_a_bare_yes_reaches_the_worker_with_the_question_and_the_rule(monkeypatch):
    created = _fake_kanban(monkeypatch)
    _route("Mets du 10 % sur ce produit", "conv-yes-1")
    router.note_nora_reply("conv-yes-1", "Je ne sais pas sur quel document ni sur quel article appliquer la remise.")
    _route("Oui, vas-y.", "conv-yes-1")
    body = created[-1]["body"]
    assert "NORA: Je ne sais pas sur quel document" in body, "the question NORA asked is in the film"
    assert "« oui » does not answer it" in body


def test_the_same_reply_is_filmed_once():
    router.note_nora_reply("conv-dup", "Voici le changement que je propose.")
    router.note_nora_reply("conv-dup", "Voici le changement que je propose.")
    assert router._CONV_HISTORY["conv-dup"].count("NORA: Voici le changement que je propose.") == 1


# ── every reply the desk shows enters the film ───────────────────────────────────────

def test_a_reply_delivered_to_the_desk_enters_the_film(monkeypatch):
    import aiohttp

    from gateway.config import PlatformConfig
    from gateway.platforms.webhook import WebhookAdapter

    class _Resp:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def text(self):
            return "ok"

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        def post(self, *_a, **_kw):
            return _Resp()

    monkeypatch.setattr(aiohttp, "ClientSession", lambda *a, **kw: _Session())
    adapter = WebhookAdapter(PlatformConfig(enabled=True, extra={"routes": {}}))
    delivery = {"deliver_extra": {"callback_url": "https://erp.example.test/api/method/x",
                                  "callback_token": "tok", "conversation_id": "conv-desk"}}
    question = "Pourriez-vous me donner le nom exact du client ?"
    result = asyncio.run(adapter._deliver_nora(question, "webhook:nora_chat:conv-desk:1", delivery))
    assert result.success
    assert router._CONV_HISTORY["conv-desk"][-1] == "NORA: " + question
