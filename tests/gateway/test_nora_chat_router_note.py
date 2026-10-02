# //// Neoffice — added file (no upstream equivalent): a note for the person asking is written in
# //// code by nora (notes.route_note), never left to a pole or to the model (#1040).
"""« Crée une note : … » is written by nora in code, whatever pole its words would reach.

Before, « Crée-moi une note : penser à commander les joints pour le chantier … » reached the
projet pole (the word « chantier »), whose only note tool writes a project's diary, and
« Prends note : … » reached the orchestrator, which answered « C'est noté » with nothing written.
note_request_vectors.json is shared byte for byte with nora's tests: both sides read the same words.
"""
import io
import json
import time
from pathlib import Path

import pytest

from gateway import nora_chat_router as R

VECTORS = json.loads((Path(__file__).parent / "note_request_vectors.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("message", [m for m, _note in VECTORS["notes"]] + VECTORS["asks"])
def test_a_note_request_is_recognised(message):
    assert R._is_note_request(message)


@pytest.mark.parametrize("message", VECTORS["not_notes"])
def test_these_are_not_note_requests(message):
    assert not R._is_note_request(message)


@pytest.mark.parametrize("message", VECTORS["reminders"])
def test_a_note_with_a_moment_stays_a_one_off_reminder(message):
    assert not R._is_note_request(message)
    assert R._fast_path(message, prior=None) == "DIRECT"


# ── the route: nora writes the note, the gateway delivers its answer ──────────────────

_CB = "https://erp.example.test/api/method/nora.api.v2.hermes_callback.deliver"
_EXTRA = {"callback_url": _CB, "callback_token": "tok", "conversation_id": "conv-note"}
_ACK = "C'est noté dans vos notes : « Penser à commander les joints pour le chantier de la rue du Lac »."


class _Http:
    """Stands in for urllib.request.urlopen; records every POST and answers per URL suffix."""

    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def __call__(self, req, timeout=None):
        body = json.loads(req.data.decode())
        self.calls.append((req.full_url, body))
        answer = next((a for suffix, a in self.answers.items() if req.full_url.endswith(suffix)), None)
        if isinstance(answer, Exception):
            raise answer
        return _Resp(io.BytesIO(json.dumps(answer or {}).encode()))


class _Resp:
    def __init__(self, buf):
        self._buf, self.status = buf, 200

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return self._buf.getvalue()


@pytest.fixture(autouse=True)
def fresh_state():
    R._LAST_ROUTE.clear()
    R._PENDING_NOTE.clear()
    R._CONV_HISTORY.clear()
    yield
    R._LAST_ROUTE.clear()
    R._PENDING_NOTE.clear()
    R._CONV_HISTORY.clear()


def _no_llm(**_kw):
    raise AssertionError("a note is decided without the classifier")


def _route(http, monkeypatch, message, *, extra=_EXTRA, page_context=None, call_llm_fn=_no_llm):
    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", http)
    return R.route_chat_message(
        message=message, session_chat_id="webhook:nora_chat:test", conversation_id="conv-note",
        thread_id=None, user_id="u", notifier_profile="default", idempotency_key="k",
        call_llm_fn=call_llm_fn, main_runtime=None, deliver_extra=extra, chat_user="staff@example.test",
        page_context=page_context)


def _written(ack=_ACK, **more):
    return _Http({"notes.route_note": {"message": {"ok": True, "note": "VN-2026-00100", "ack": ack, **more}},
                  "hermes_callback.deliver": {"message": "ok"}})


def test_the_note_is_written_by_nora_and_its_answer_delivered(monkeypatch):
    http = _written()
    message = "Crée-moi une note : penser à commander les joints pour le chantier de la rue du Lac."
    decision = _route(http, monkeypatch, message)
    assert decision["routed"] is True and decision["note"] == "VN-2026-00100" and decision["ack"] == _ACK
    # Two POSTs, no more: neither the classifier nor the read-only fast-answer engine is asked.
    (url, body), (ack_url, ack_body) = http.calls
    assert url.endswith("nora.api.v2.notes.route_note")
    assert body == {"user": "staff@example.test", "message": message, "conversation_id": "conv-note",
                    "follow_up": False}
    assert ack_url == _CB and ack_body == {"conversation_id": "conv-note", "text": _ACK}


@pytest.mark.parametrize("page_context", (
    {"source": "nora-live-widget", "pole_hint": "compta"},   # the pole the voice named
    {"doctype": "Project", "name": "PROJ-0001", "route": "Form/Project/PROJ-0001"},  # a job page
))
def test_neither_the_voice_pole_nor_a_job_page_takes_a_note(monkeypatch, page_context):
    http = _written()
    decision = _route(http, monkeypatch, "Prends note : le fournisseur de joints ne livre que le mardi.",
                      page_context=page_context)
    assert decision["routed"] is True
    assert http.calls[0][0].endswith("notes.route_note")


def test_a_projet_conversation_keeps_its_job_note_when_no_job_page_is_open(monkeypatch):
    R._LAST_ROUTE["conv-note"] = {"msg": "statut du chantier PROJ-0001", "pole": "projet"}
    # The classifier then decides as before (here: DIRECT, so no kanban task is made in a test).
    monkeypatch.setattr(R, "classify", lambda *a, **k: "DIRECT")
    http = _written()
    _route(http, monkeypatch, "Note : il faudra refaire le joint du brûleur")
    assert not any(url.endswith("notes.route_note") for url, _b in http.calls)


def test_on_a_job_page_a_projet_conversation_writes_the_note_itself(monkeypatch):
    R._LAST_ROUTE["conv-note"] = {"msg": "statut du chantier PROJ-0001", "pole": "projet"}
    http = _written()
    decision = _route(http, monkeypatch, "Note : il faudra refaire le joint du brûleur",
                      page_context={"doctype": "Project", "name": "PROJ-0001"})
    assert decision["routed"] is True and http.calls[0][0].endswith("notes.route_note")


def test_nothing_to_note_asks_and_the_next_message_is_the_note(monkeypatch):
    ask = "Bien sûr. Que voulez-vous que je note ?"
    http = _Http({"notes.route_note": {"message": {"ok": True, "asked": True, "ack": ask}},
                  "hermes_callback.deliver": {"message": "ok"}})
    decision = _route(http, monkeypatch, "Peux-tu me créer une note ?")
    assert decision["routed"] is True and decision["asked"] is True and decision["ack"] == ask
    assert "conv-note" in R._PENDING_NOTE

    http2 = _written(ack="C'est noté dans vos notes : « Rappeler Dupont pour le devis ».")
    decision = _route(http2, monkeypatch, "Rappeler Dupont pour le devis")
    assert decision["routed"] is True
    url, body = http2.calls[0]
    assert url.endswith("notes.route_note") and body["follow_up"] is True
    assert body["message"] == "Rappeler Dupont pour le devis"
    assert "conv-note" not in R._PENDING_NOTE  # the question is answered once


@pytest.mark.parametrize("reply", ("Non merci", "Laisse tomber", "Quel est le chiffre d'affaires de septembre ?"))
def test_a_refusal_or_a_question_is_not_the_note(monkeypatch, reply):
    R._PENDING_NOTE["conv-note"] = time.time()
    assert R._take_pending_note("conv-note", reply) is False
    assert "conv-note" not in R._PENDING_NOTE


def test_the_question_does_not_stay_open_forever():
    R._PENDING_NOTE["conv-note"] = time.time() - R._PENDING_NOTE_TTL - 1
    assert R._take_pending_note("conv-note", "Rappeler Dupont pour le devis") is False


def test_a_declined_note_goes_to_the_orchestrator_with_its_tool(monkeypatch):
    http = _Http({"notes.route_note": {"message": {"ok": False, "error": "notes are not installed on this site"}}})
    decision = _route(http, monkeypatch, "Crée une note : vérifier la TVA du trimestre")
    assert decision["routed"] is False and decision["category"] == "DIRECT"
    hint = decision["agent_hint"]
    assert "nora_note_create" in hint and "do NOT call kanban_create" in hint
    assert len(http.calls) == 1  # nothing delivered: the orchestrator answers


def test_without_a_desk_callback_the_orchestrator_writes_the_note(monkeypatch):
    http = _Http({})
    decision = _route(http, monkeypatch, "Crée une note : vérifier la TVA du trimestre", extra=None)
    assert decision["routed"] is False and "nora_note_create" in decision["agent_hint"]
    assert http.calls == []
