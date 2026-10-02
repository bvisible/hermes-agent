# //// Neoffice — added file (no upstream equivalent): composing a person's space is NORA's own
# //// conversation, routed to her with its instruction (step 3 of the spaces composed with NORA).
"""« Ajoute les bons de livraison à mon espace Commercial » is NORA's, not the pole of the document.

Her server holds the space tools, and nora keeps a change only after the person's yes to the bar shown;
the words of the request name documents and would send it to their pole. The turns that follow it (the
yes, a further change) stay hers for ten minutes; any other turn ends the conversation.
"""
import time

import pytest

from gateway import nora_chat_router as R

SPACE_REQUESTS = [
    "Ajoute les bons de livraison à mon espace Commercial",
    "Crée-moi un espace Garage avec les ordres de réparation",
    "Cache l'onglet Prospects de mon espace Commercial",
    "Renomme mon espace Commercial en Ventes",
    "Quelle icône je pourrais mettre à mon espace Atelier ?",
    "Qu'est-ce que je pourrais ajouter à mon espace Finance ?",
    "Mets les factures en premier dans mon espace de travail",
    "Füge die Lieferscheine zu meinem Arbeitsbereich hinzu",
    "Aggiungi le bolle di consegna al mio spazio di lavoro",
    "Add delivery notes to my workspace",
]
NOT_SPACE = [
    "Ajoute une ligne à la facture FA-2026-00012",
    "Combien d'espace disque reste-t-il ?",
    "Crée un espace client pour la boulangerie",
    "Montre-moi les factures en retard",
    "Quel rapport me permet de suivre ce que mes clients me doivent ?",
]


@pytest.mark.parametrize("message", SPACE_REQUESTS)
def test_a_space_request_is_recognised(message):
    assert R._is_space_request(message)


@pytest.mark.parametrize("message", NOT_SPACE)
def test_these_are_not_space_requests(message):
    assert not R._is_space_request(message)


@pytest.fixture(autouse=True)
def fresh_state():
    for d in (R._LAST_ROUTE, R._PENDING_SPACE, R._PENDING_NOTE, R._CONV_HISTORY):
        d.clear()
    yield
    for d in (R._LAST_ROUTE, R._PENDING_SPACE, R._PENDING_NOTE, R._CONV_HISTORY):
        d.clear()


def _no_llm(**_kw):
    raise AssertionError("a space turn is decided without the classifier")


def _route(message, call_llm_fn=_no_llm, extra=None):
    return R.route_chat_message(
        message=message, session_chat_id="webhook:nora_chat:test", conversation_id="conv-space",
        thread_id=None, user_id="u", notifier_profile="default", idempotency_key="k",
        call_llm_fn=call_llm_fn, main_runtime=None, deliver_extra=extra, chat_user="staff@example.test",
        page_context=None)


def test_the_request_goes_to_nora_with_its_instruction():
    decision = _route("Ajoute les bons de livraison à mon espace Commercial")
    assert decision["routed"] is False and decision["category"] == "DIRECT"
    assert decision["agent_hint"] == R._SPACE_HINT
    assert "conv-space" in R._PENDING_SPACE


@pytest.mark.parametrize("follow_up", ("Oui", "Oui, vas-y", "Et cache aussi les prospects",
                                       "Plutôt l'icône de la voiture", "Non, laisse tomber"))
def test_the_turns_that_follow_stay_hers(follow_up):
    _route("Ajoute les bons de livraison à mon espace Commercial")
    decision = _route(follow_up)
    assert decision["category"] == "DIRECT" and decision["agent_hint"] == R._SPACE_HINT


def test_another_question_ends_the_space_conversation(monkeypatch):
    _route("Ajoute les bons de livraison à mon espace Commercial")
    asked = []
    monkeypatch.setattr(R, "classify", lambda message, **kw: asked.append(message) or "DIRECT")
    monkeypatch.setattr(R, "_asks_fast_answer", lambda *a, **k: False)
    decision = _route("Combien de factures avons-nous émises en septembre ?")
    assert asked == ["Combien de factures avons-nous émises en septembre ?"]
    assert decision.get("agent_hint") != R._SPACE_HINT
    assert "conv-space" not in R._PENDING_SPACE


def test_the_conversation_does_not_stay_hers_forever():
    R._PENDING_SPACE["conv-space"] = time.time() - R._PENDING_SPACE_TTL - 1
    assert R._continues_space("conv-space", "Oui") is False


def test_a_note_about_a_space_is_a_note():
    assert R._is_note_request("Crée une note : réorganiser mon espace Commercial lundi")
    decision = _route("Crée une note : réorganiser mon espace Commercial lundi")
    assert decision.get("agent_hint") != R._SPACE_HINT
