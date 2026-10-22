# //// Neoffice — added file (no upstream equivalent): a presentation to make is the orchestrator's own turn, with the
# //// presentation tool, decided before every other rule (22.10, step 4 of « Nora partout »).
"""« Fais-moi une présentation de 5 diapositives sur nos services » goes to the orchestrator, who holds
nora_presentation_create; no pole holds it.

Before, a presentation matched no rule: the classifier, which has no presentation class, decided, and « avec un
graphique des ventes » reached analyse through the chart rule. A question about an existing presentation (« où est la
présentation de la société ? »), how to make one, NORA presenting herself, a recurring job, an e-mail or a task stay
what they were. From an open NeoPrés the turn is the editor's: the deck is there. All names are invented.
"""
import io
import json
import re
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway import nora_chat_router as R

VECTORS = json.loads((Path(__file__).parent / "deck_request_vectors.json").read_text(encoding="utf-8"))
LETTERS = json.loads((Path(__file__).parent / "letter_request_vectors.json").read_text(encoding="utf-8"))
_CB = "https://erp.example.test/api/method/nora.api.v2.hermes_callback.deliver"
_EXTRA = {"callback_url": _CB, "callback_token": "tok", "conversation_id": "conv-deck"}
_WORD = {"surface": "editor", "app": "word", "document": {"name": "Brouillon.docx", "pages": 1}}
_DECK = {"surface": "editor", "app": "deck", "document": {"name": "Offre.pptx", "pages": 4}}


@pytest.fixture(autouse=True)
def fresh_state():
    for d in (R._LAST_ROUTE, R._PENDING_SPACE, R._PENDING_NOTE, R._CONV_HISTORY, R._SPACE_ASKED):
        d.clear()
    yield
    for d in (R._LAST_ROUTE, R._PENDING_SPACE, R._PENDING_NOTE, R._CONV_HISTORY, R._SPACE_ASKED):
        d.clear()


@pytest.mark.parametrize("message", VECTORS["decks"])
def test_a_presentation_to_make_is_recognised(message):
    assert R._is_deck_request(message), message


@pytest.mark.parametrize("message", VECTORS["not_decks"] + VECTORS["capability"])
def test_these_are_not_a_presentation_to_make(message):
    assert not R._is_deck_request(message), message


@pytest.mark.parametrize("message", VECTORS["capability"])
def test_the_question_about_what_nora_can_do_stays_a_capability_question(message):
    assert R._fast_path(message, prior=None) == "DIRECT", message


@pytest.mark.parametrize("message", LETTERS["letters"] + LETTERS["dunning"])
def test_a_letter_or_a_reminder_is_never_taken_for_a_presentation(message):
    assert not R._is_deck_request(message), message


def test_the_hint_names_only_nora_tools():
    # det/67 holds every nora_* tool named in a *_HINT to the orchestrator's own server.
    assert set(re.findall(r"\bnora_[a-z_]+", R._DECK_HINT)) == {"nora_presentation_create", "nora_company_details"}


# ── the route ─────────────────────────────────────────────────────────────────────────────────────────────────────


class _Http:
    def __init__(self):
        self.calls = []

    def __call__(self, req, timeout=None):
        self.calls.append((req.full_url, json.loads(req.data.decode())))
        return _Resp(io.BytesIO(json.dumps({"message": "ok"}).encode()))


class _Resp:
    def __init__(self, buf):
        self._buf = buf

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return self._buf.getvalue()


def _fake_kanban(monkeypatch):
    created = []
    kanban_db = types.SimpleNamespace(create_task=lambda conn, **kw: created.append(kw) or "t_deck")
    connect = types.SimpleNamespace(connect=lambda board=None: SimpleNamespace(close=lambda: None))
    notify = types.SimpleNamespace(add_notify_sub=lambda *a, **kw: None)
    pkg = sys.modules.get("hermes_cli") or types.ModuleType("hermes_cli")
    monkeypatch.setitem(sys.modules, "hermes_cli", pkg)
    for name, mod in (("kanban_db", kanban_db), ("kanban_db_connect", connect), ("kanban_db_notify", notify)):
        monkeypatch.setattr(pkg, name, mod, raising=False)
        monkeypatch.setitem(sys.modules, f"hermes_cli.{name}", mod)
    return created


def _route(monkeypatch, message, page_context=None, classify_to=None, fast_answer="a read the fast answer must never give"):
    import urllib.request

    asked = []
    monkeypatch.setattr(urllib.request, "urlopen", _Http())
    monkeypatch.setattr(R, "_asks_fast_answer", lambda *a, **k: True)
    monkeypatch.setattr(R, "_fast_answer", lambda *a, **k: asked.append(a) or fast_answer)

    def classifier(**_kw):
        if classify_to is None:
            pytest.fail("a presentation is decided without the classifier")
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=classify_to))])

    decision = R.route_chat_message(
        message=message, session_chat_id="webhook:nora_chat:test", conversation_id="conv-deck",
        thread_id=None, user_id="u", notifier_profile="default", idempotency_key="k",
        call_llm_fn=classifier, main_runtime=None, deliver_extra=dict(_EXTRA), chat_user="staff@example.test",
        language="fr", page_context=page_context, nora_spoke=True)
    return decision, asked


def test_the_desk_presentation_is_the_orchestrators_turn_with_the_tool(monkeypatch):
    created = _fake_kanban(monkeypatch)
    decision, asked = _route(monkeypatch, VECTORS["decks"][0])
    assert created == [], "no pole holds the presentation tool: no kanban task"
    assert asked == [], "a presentation is a gesture: the fast answer is not asked"
    assert decision["routed"] is False and decision["category"] == "DIRECT"
    assert decision["agent_hint"] == R._DECK_HINT


def test_a_chart_in_the_presentation_does_not_send_it_to_analyse(monkeypatch):
    message = "Fais-moi une présentation avec un graphique des ventes du trimestre"
    assert R._keyword_pole(message) == "analyse", "the chart rule alone would take it"
    created = _fake_kanban(monkeypatch)
    decision, _asked = _route(monkeypatch, message)
    assert created == [] and decision["category"] == "DIRECT" and decision["agent_hint"] == R._DECK_HINT


def test_a_presentation_on_a_job_page_stays_a_presentation(monkeypatch):
    created = _fake_kanban(monkeypatch)
    decision, _asked = _route(monkeypatch, "Prépare une présentation sur le chantier pour le client",
                              page_context={"job": {"project": "PROJ-0001"}, "project": "PROJ-0001"})
    assert created == [] and decision["category"] == "DIRECT" and decision["agent_hint"] == R._DECK_HINT


def test_a_presentation_asked_from_an_open_document_gets_both_instructions(monkeypatch):
    _fake_kanban(monkeypatch)
    decision, _asked = _route(monkeypatch, "Fais-moi une présentation sur ce document", page_context=_WORD)
    hint = decision["agent_hint"]
    assert R._editor_hint({"app": "word", "name": "Brouillon.docx"}) in hint and R._DECK_HINT in hint


def test_from_an_open_presentation_it_is_the_editors_turn(monkeypatch):
    _fake_kanban(monkeypatch)
    decision, _asked = _route(monkeypatch, "Fais-moi 3 diapositives sur nos tarifs", page_context=_DECK)
    hint = decision["agent_hint"]
    assert R._editor_hint({"app": "deck", "name": "Offre.pptx"}) in hint and R._DECK_HINT not in hint


def test_a_question_about_the_companys_presentation_is_classified_as_before(monkeypatch):
    created = _fake_kanban(monkeypatch)
    decision, _asked = _route(monkeypatch, "Où est la présentation de la société ?", classify_to="support",
                              fast_answer=None)
    assert R._DECK_HINT not in (decision.get("agent_hint") or "")
    assert decision["category"] == "support" and created and R._DECK_HINT not in created[-1]["body"]


def test_a_presentation_beats_the_letter(monkeypatch):
    _fake_kanban(monkeypatch)
    decision, _asked = _route(monkeypatch, "Prépare une présentation et une lettre pour la régie Fictive")
    assert decision["agent_hint"] == R._DECK_HINT and R._LETTER_HINT not in decision["agent_hint"]
