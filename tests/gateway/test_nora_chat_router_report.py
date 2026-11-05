# //// Neoffice — added file (no upstream equivalent): a report to make is the orchestrator's own turn, with the report
# //// tool, decided before every other rule (05.11).
"""« Fais-moi un rapport sur nos ventes 2026 » goes to the orchestrator, who holds nora_report_create; no pole holds it.

Before, it reached compta, which tells a figure in the chat or runs an ERP report. « Rapport » is a hard word: an ERP
report to run (« sors le rapport des ventes », « un rapport TVA »), which report answers a need, « par rapport à », how
to make one, a workbook, a recurring job, an e-mail or a task stay what they were. From an open NeoDoc the turn is the
editor's. All names are invented.
"""
import io
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway import nora_chat_router as R

VECTORS = json.loads((Path(__file__).parent / "report_request_vectors.json").read_text(encoding="utf-8"))
DECKS = json.loads((Path(__file__).parent / "deck_request_vectors.json").read_text(encoding="utf-8"))
SHEETS = json.loads((Path(__file__).parent / "sheet_request_vectors.json").read_text(encoding="utf-8"))
LETTERS = json.loads((Path(__file__).parent / "letter_request_vectors.json").read_text(encoding="utf-8"))
_CB = "https://erp.example.test/api/method/nora.api.v2.hermes_callback.deliver"
_EXTRA = {"callback_url": _CB, "callback_token": "tok", "conversation_id": "conv-report"}
_WORD = {"surface": "editor", "app": "word", "document": {"name": "Brouillon.docx", "pages": 1}}


@pytest.fixture(autouse=True)
def fresh_state():
    stores = (R._LAST_ROUTE, R._PENDING_SPACE, R._PENDING_NOTE, R._CONV_HISTORY, R._SPACE_ASKED, R._DECK_TURNS, R._REPORT_TURNS)
    for store in stores:
        store.clear()
    yield
    for store in stores:
        store.clear()


@pytest.mark.parametrize("message", VECTORS["reports"])
def test_a_report_to_make_is_recognised(message):
    assert R._is_report_request(message), message


@pytest.mark.parametrize("message", VECTORS["not_reports"] + VECTORS["capability"])
def test_these_are_not_a_report_to_make(message):
    assert not R._is_report_request(message), message


@pytest.mark.parametrize("message", DECKS["decks"] + DECKS["not_decks"])
def test_a_presentation_is_never_taken_for_a_report(message):
    assert not R._is_report_request(message) or "rapport" in message.lower(), message


@pytest.mark.parametrize("message", LETTERS["letters"] + LETTERS["dunning"])
def test_a_letter_is_never_taken_for_a_report(message):
    assert not R._is_report_request(message), message


def test_the_hint_names_only_nora_tools():
    # det/67 holds every nora_* tool named in a *_HINT to the orchestrator's own server.
    assert set(re.findall(r"\bnora_[a-z_]+", R._REPORT_HINT)) == {"nora_report_create", "nora_company_details"}


class _Resp:
    def __init__(self, buf):
        self._buf = buf

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return self._buf.getvalue()


def _route(monkeypatch, message, page_context=None, classify_to=None, key="k"):
    import urllib.request

    asked = []
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda req, timeout=None: _Resp(io.BytesIO(json.dumps({"message": "ok"}).encode()))
    )
    monkeypatch.setattr(R, "_asks_fast_answer", lambda *a, **k: True)
    monkeypatch.setattr(R, "_fast_answer", lambda *a, **k: asked.append(a) or "a read the fast answer must never give")

    def classifier(**_kw):
        if classify_to is None:
            pytest.fail("a report is decided without the classifier")
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=classify_to))])

    decision = R.route_chat_message(
        message=message, session_chat_id="webhook:nora_chat:test", conversation_id="conv-report",
        thread_id=None, user_id="u", notifier_profile="default", idempotency_key=key,
        call_llm_fn=classifier, main_runtime=None, deliver_extra=dict(_EXTRA), chat_user="staff@example.test",
        language="fr", page_context=page_context, nora_spoke=True)
    return decision, asked


def test_the_desk_report_is_the_orchestrators_turn_with_the_tool(monkeypatch):
    decision, asked = _route(monkeypatch, "Fais-moi un rapport sur nos ventes 2026")
    assert asked == [], "a report is a gesture: the fast answer is not asked"
    assert decision["routed"] is False and decision["category"] == "DIRECT"
    assert decision["agent_hint"] == R._REPORT_HINT


def test_its_turn_is_marked_for_kanban_create(monkeypatch):
    _route(monkeypatch, "Fais-moi un rapport sur nos ventes 2026", key="delivery-7")
    assert R.report_turn_under_way("delivery-7")
    assert not R.deck_turn_under_way("delivery-7"), "the refusal must speak of a report"
    assert not R.report_turn_under_way("delivery-8")


def test_a_mark_lapses(monkeypatch):
    _route(monkeypatch, "Fais-moi un rapport sur nos ventes 2026", key="delivery-9")
    now = R._time_thread.time()
    monkeypatch.setattr(R._time_thread, "time", lambda: now + R._DECK_TURN_TTL + 1)
    assert not R.report_turn_under_way("delivery-9")


def test_a_presentation_of_a_report_stays_a_presentation(monkeypatch):
    decision, _asked = _route(monkeypatch, "Fais-moi une présentation du rapport annuel")
    assert decision["agent_hint"] == R._DECK_HINT


def test_from_an_open_neodoc_it_is_the_editors_turn(monkeypatch):
    decision, _asked = _route(monkeypatch, "Fais-moi un rapport sur nos ventes 2026", page_context=_WORD)
    assert "nora_report_create" not in (decision.get("agent_hint") or "")
