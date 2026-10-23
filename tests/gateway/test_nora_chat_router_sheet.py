# //// Neoffice — added file (no upstream equivalent): a workbook of the company's figures to make is the orchestrator's
# //// own turn, with the sheet tool, decided like a presentation (23.10, step 5 of « Nora partout »).
"""« Fais-moi un classeur des ventes par mois de 2026 » goes to the orchestrator, who holds nora_sheet_create; no pole
holds it. Before, it matched the revenue rule and reached compta, which tells a figure in the chat and makes no file.

What stays what it was: a table asked in the chat (« fais-moi un tableau des ventes par mois »: no file named), a
question about a figure, a dashboard, a chart (the chart rule's), how to export one, an e-mail, a task, a file the
person already has, a dunning, a recurring job. From an open NeoGrille the turn is the editor's: the figures go into
the open sheet (nora_sheet_fill), and « fais un graphique » there stays the editor's. All names are invented.
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

VECTORS = json.loads((Path(__file__).parent / "sheet_request_vectors.json").read_text(encoding="utf-8"))
LETTERS = json.loads((Path(__file__).parent / "letter_request_vectors.json").read_text(encoding="utf-8"))
DECKS = json.loads((Path(__file__).parent / "deck_request_vectors.json").read_text(encoding="utf-8"))
_CB = "https://erp.example.test/api/method/nora.api.v2.hermes_callback.deliver"
_EXTRA = {"callback_url": _CB, "callback_token": "tok", "conversation_id": "conv-sheet"}
_WORD = {"surface": "editor", "app": "word", "document": {"name": "Brouillon.docx", "pages": 1}}
_SHEET = {"surface": "editor", "app": "sheet", "document": {"name": "Ventes.xlsx"}}


@pytest.fixture(autouse=True)
def fresh_state():
    stores = (R._LAST_ROUTE, R._PENDING_SPACE, R._PENDING_NOTE, R._CONV_HISTORY, R._SPACE_ASKED, R._DECK_TURNS,
              R._SHEET_TURNS, R._SHEET_QUESTIONS, R._SHEET_OFFERS)
    for d in stores:
        d.clear()
    yield
    for d in stores:
        d.clear()


@pytest.mark.parametrize("message", VECTORS["sheets"])
def test_a_workbook_to_make_is_recognised(message):
    assert R._is_sheet_request(message), message


@pytest.mark.parametrize("message", VECTORS["not_sheets"] + VECTORS["recurring_sheets"])
def test_these_are_not_a_workbook_to_make(message):
    assert not R._is_sheet_request(message), message


@pytest.mark.parametrize("message", LETTERS["letters"] + LETTERS["dunning"] + DECKS["decks"] + DECKS["not_decks"])
def test_a_letter_a_reminder_or_a_presentation_is_never_taken_for_a_workbook(message):
    assert not R._is_sheet_request(message), message


@pytest.mark.parametrize("message", VECTORS["sheet_questions"])
def test_whether_a_workbook_can_be_made_is_a_question(message):
    assert R._is_sheet_question(message), message


@pytest.mark.parametrize("message", VECTORS["not_sheet_questions"] + VECTORS["recurring_sheets"] + VECTORS["sheets"])
def test_these_are_not_a_question_about_a_workbook(message):
    assert not R._is_sheet_question(message), message


def test_the_question_hint_names_only_nora_tools_and_makes_nothing():
    assert set(re.findall(r"\bnora_[a-z_]+", R._SHEET_QUESTION_HINT)) == {"nora_sheet_create"}
    assert "nothing is made in this turn" in R._SHEET_QUESTION_HINT and "Never give a figure" in R._SHEET_QUESTION_HINT
    assert not re.findall(r"\bnora_[a-z_]+", R._SHEET_YES_HINT)


def test_the_hint_names_only_nora_tools():
    # det/67 holds every nora_* tool named in a *_HINT to the orchestrator's own server.
    assert set(re.findall(r"\bnora_[a-z_]+", R._SHEET_HINT)) == {"nora_sheet_create"}
    assert "nora_sheet_fill" in R._EDITOR_HINTS["sheet"] and "nora_sheet_create" in R._EDITOR_HINTS["sheet"]
    assert "Never kanban_create" in R._SHEET_HINT and "never write, guess or repeat a figure" in R._SHEET_HINT


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
    kanban_db = types.SimpleNamespace(create_task=lambda conn, **kw: created.append(kw) or "t_sheet")
    connect = types.SimpleNamespace(connect=lambda board=None: SimpleNamespace(close=lambda: None))
    notify = types.SimpleNamespace(add_notify_sub=lambda *a, **kw: None)
    pkg = sys.modules.get("hermes_cli") or types.ModuleType("hermes_cli")
    monkeypatch.setitem(sys.modules, "hermes_cli", pkg)
    for name, mod in (("kanban_db", kanban_db), ("kanban_db_connect", connect), ("kanban_db_notify", notify)):
        monkeypatch.setattr(pkg, name, mod, raising=False)
        monkeypatch.setitem(sys.modules, f"hermes_cli.{name}", mod)
    return created


def _route(monkeypatch, message, page_context=None, classify_to=None, fast_answer="a read the fast answer must never give",
           key="k"):
    import urllib.request

    asked = []
    monkeypatch.setattr(urllib.request, "urlopen", _Http())
    monkeypatch.setattr(R, "_asks_fast_answer", lambda *a, **k: True)
    monkeypatch.setattr(R, "_fast_answer", lambda *a, **k: asked.append(a) or fast_answer)

    def classifier(**_kw):
        if classify_to is None:
            pytest.fail("a workbook is decided without the classifier")
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=classify_to))])

    decision = R.route_chat_message(
        message=message, session_chat_id="webhook:nora_chat:test", conversation_id="conv-sheet",
        thread_id=None, user_id="u", notifier_profile="default", idempotency_key=key,
        call_llm_fn=classifier, main_runtime=None, deliver_extra=dict(_EXTRA), chat_user="staff@example.test",
        language="fr", page_context=page_context, nora_spoke=True)
    return decision, asked


def test_the_desk_workbook_is_the_orchestrators_turn_with_the_tool(monkeypatch):
    message = VECTORS["sheets"][0]
    created = _fake_kanban(monkeypatch)
    decision, asked = _route(monkeypatch, message, key="delivery-1")
    assert created == [], "no pole holds the sheet tool: no kanban task"
    assert asked == [], "a workbook is a gesture: the fast answer is not asked"
    assert decision["routed"] is False and decision["category"] == "DIRECT"
    assert decision["agent_hint"] == R._SHEET_HINT
    assert R.sheet_turn_under_way("delivery-1") and not R.deck_turn_under_way("delivery-1")


def test_a_table_in_the_chat_is_answered_as_before(monkeypatch):
    _fake_kanban(monkeypatch)
    decision, _asked = _route(monkeypatch, "Fais-moi un tableau des ventes par mois", classify_to="compta",
                              fast_answer=None, key="delivery-2")
    assert R._SHEET_HINT not in (decision.get("agent_hint") or "")
    assert not R.sheet_turn_under_way("delivery-2")


def test_from_an_open_neogrille_it_is_the_editors_turn_with_the_fill_recipe(monkeypatch):
    _fake_kanban(monkeypatch)
    for message in ("Remplis A1 avec les ventes par mois de cette année",
                    "Fais-moi un classeur des ventes par mois de 2026",
                    "Fais un graphique des ventes"):
        decision, _asked = _route(monkeypatch, message, page_context=_SHEET, key="delivery-3")
        hint = decision["agent_hint"]
        assert R._editor_hint({"app": "sheet", "name": "Ventes.xlsx"}) in hint, message
        assert R._SHEET_HINT not in hint, message
        assert "nora_sheet_fill" in hint, message
        assert not R.sheet_turn_under_way("delivery-3"), message


def test_a_workbook_asked_from_an_open_text_gets_both_instructions(monkeypatch):
    _fake_kanban(monkeypatch)
    decision, _asked = _route(monkeypatch, "Fais-moi un classeur des ventes par mois de 2026", page_context=_WORD)
    hint = decision["agent_hint"]
    assert R._editor_hint({"app": "word", "name": "Brouillon.docx"}) in hint and R._SHEET_HINT in hint


def test_a_presentation_of_the_sales_stays_a_presentation(monkeypatch):
    _fake_kanban(monkeypatch)
    decision, _asked = _route(monkeypatch, "Prépare une présentation et un classeur des ventes de 2026")
    assert decision["agent_hint"] == R._DECK_HINT


def test_a_chart_still_goes_to_analyse(monkeypatch):
    message = "Fais-moi un graphique des ventes en Excel"
    assert R._keyword_pole(message) == "analyse"
    assert not R._is_sheet_request(message)


def test_the_mark_lapses_and_a_turn_without_an_id_is_never_marked(monkeypatch):
    R._mark_sheet_turn("delivery-9")
    started = R._SHEET_TURNS["delivery-9"]
    monkeypatch.setattr(R._time_thread, "time", lambda: started + R._DECK_TURN_TTL + 1)
    assert not R.sheet_turn_under_way("delivery-9")
    R._mark_sheet_turn("")
    assert "" not in R._SHEET_TURNS


# ── a question makes nothing; the yes that follows does (23.10) ───────────────────────────────────────────────────


def test_a_question_is_answered_by_nora_and_nothing_is_made(monkeypatch):
    created = _fake_kanban(monkeypatch)
    decision, asked = _route(monkeypatch, "Est-il possible d'exporter les ventes en Excel ?", key="delivery-q1")
    assert created == [] and asked == [], "no pole, no fast answer: NORA answers it"
    assert decision["routed"] is False and decision["category"] == "DIRECT"
    assert decision["agent_hint"] == R._SHEET_QUESTION_HINT
    assert R.sheet_question_under_way("delivery-q1") and not R.sheet_turn_under_way("delivery-q1")
    assert R.tool_refused_in_turn("nora_sheet_create", "delivery-q1") == R.SHEET_QUESTION_TOOL_REFUSAL
    assert R.tool_refused_in_turn("nora_company_details", "delivery-q1") is None
    assert R.tool_refused_in_turn("nora_sheet_create", "delivery-other") is None


def test_a_question_with_a_making_verb_is_never_read_as_a_request(monkeypatch):
    message = "Est-ce que je peux exporter les débiteurs en Excel ?"
    assert not R._is_sheet_request(message), "« exporter » follows the question: it asks, it does not order"
    _fake_kanban(monkeypatch)
    decision, _asked = _route(monkeypatch, message, key="delivery-q8")
    assert decision["agent_hint"] == R._SHEET_QUESTION_HINT
    assert R.sheet_question_under_way("delivery-q8") and not R.sheet_turn_under_way("delivery-q8")


@pytest.mark.parametrize("yes", ["Oui, volontiers", "Oui merci", "Ok merci", "Yes please", "Ja, bitte", "Yes, no problem"])
def test_the_yes_that_follows_makes_the_workbook(monkeypatch, yes):
    _fake_kanban(monkeypatch)
    _route(monkeypatch, "Est-il possible d'exporter les ventes en Excel ?", key="delivery-q2")
    decision, asked = _route(monkeypatch, yes, key="delivery-q3")
    assert asked == []
    assert decision["category"] == "DIRECT" and decision.get("ack") is None, "never a canned answer"
    assert R._SHEET_HINT in decision["agent_hint"] and R._SHEET_YES_HINT in decision["agent_hint"]
    assert R.sheet_turn_under_way("delivery-q3") and not R.sheet_question_under_way("delivery-q3")
    assert R.tool_refused_in_turn("nora_sheet_create", "delivery-q3") is None


def test_a_no_ends_the_offer(monkeypatch):
    _fake_kanban(monkeypatch)
    _route(monkeypatch, "Est-il possible d'exporter les ventes en Excel ?", key="delivery-q9")
    decision, _asked = _route(monkeypatch, "Non merci", classify_to="direct", fast_answer=None, key="delivery-q10")
    assert R._SHEET_HINT not in (decision.get("agent_hint") or "")
    assert not R.sheet_turn_under_way("delivery-q10") and not R._sheet_offer_open("conv-sheet")


def test_another_message_ends_the_offer(monkeypatch):
    _fake_kanban(monkeypatch)
    _route(monkeypatch, "Est-il possible d'exporter les ventes en Excel ?", key="delivery-q4")
    _route(monkeypatch, "Combien avons-nous vendu en mars ?", classify_to="compta", fast_answer=None, key="delivery-q5")
    decision, _asked = _route(monkeypatch, "Oui", classify_to="direct", fast_answer=None, key="delivery-q6")
    assert R._SHEET_HINT not in (decision.get("agent_hint") or "")
    assert not R.sheet_turn_under_way("delivery-q6")


def test_the_offer_lapses(monkeypatch):
    _fake_kanban(monkeypatch)
    _route(monkeypatch, "Est-il possible d'exporter les ventes en Excel ?", key="delivery-q7")
    started = R._SHEET_OFFERS["conv-sheet"]
    monkeypatch.setattr(R._time_thread, "time", lambda: started + R._DECK_TURN_TTL + 1)
    assert not R.sheet_question_under_way("delivery-q7")
    assert not R._take_sheet_offer("conv-sheet")


def test_a_yes_while_the_chat_log_lags_gets_no_rule_about_a_new_thread(monkeypatch):
    """nora_spoke False: the chat log has not caught up with NORA's offer yet; she asked all the same."""
    _fake_kanban(monkeypatch)
    _route(monkeypatch, "Est-il possible d'exporter les ventes en Excel ?", key="delivery-q11")
    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", _Http())
    decision = R.route_chat_message(
        message="Oui", session_chat_id="webhook:nora_chat:test", conversation_id="conv-sheet", thread_id=None,
        user_id="u", notifier_profile="default", idempotency_key="delivery-q12", call_llm_fn=None, main_runtime=None,
        deliver_extra=dict(_EXTRA), chat_user="staff@example.test", language="fr", page_context=None, nora_spoke=False)
    assert R._SHEET_YES_HINT in decision["agent_hint"] and R._FRESH_THREAD_HINT not in decision["agent_hint"]
