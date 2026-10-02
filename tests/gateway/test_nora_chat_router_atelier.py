# //// Neoffice — added file (no upstream equivalent): what the person writes in the ATELIER (the theme's full
# //// screen to compose a space) is about that space, whatever its words.
"""« Masque les abonnements, je ne m'en sers pas », written in the atelier, went to the Support pole.

The atelier's NORA box sends every message with the page context {atelier: {space, label, scope}}. The router
sends each such turn to nora's space route with it, where the request is read in code and only proposed (the
atelier shows the proposal and the person keeps each change there); what nora does not read reaches the
orchestrator with the atelier's own instruction: propose, never confirm.
"""
import io
import json

import pytest

from gateway import nora_chat_router as R

_CB = "https://erp.example.test/api/method/nora.api.v2.hermes_callback.deliver"
_EXTRA = {"callback_url": _CB, "callback_token": "tok", "conversation_id": "conv-atelier"}
_ATELIER = {"space": "Commercial", "label": "Ventes", "scope": "user"}
_PAGE = {"route": "app/selling", "atelier": _ATELIER}
_PROPOSED = "Je vous propose de masquer « Abonnements » : gardez-le dans l'atelier si cela vous convient."


@pytest.fixture(autouse=True)
def fresh_state():
    for d in (R._LAST_ROUTE, R._PENDING_SPACE, R._PENDING_NOTE, R._CONV_HISTORY, R._SPACE_ASKED):
        d.clear()
    yield
    for d in (R._LAST_ROUTE, R._PENDING_SPACE, R._PENDING_NOTE, R._CONV_HISTORY, R._SPACE_ASKED):
        d.clear()


class _Http:
    """Stands in for urllib.request.urlopen; records every POST and answers per URL suffix."""

    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def __call__(self, req, timeout=None):
        self.calls.append((req.full_url, json.loads(req.data.decode())))
        answer = next((a for suffix, a in self.answers.items() if req.full_url.endswith(suffix)), None)
        return _Resp(io.BytesIO(json.dumps(answer or {}).encode()))


class _Resp:
    def __init__(self, buf):
        self._buf = buf

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return self._buf.getvalue()


def _nora(answer):
    return _Http({"space_route.route_space": {"message": answer}, "hermes_callback.deliver": {"message": "ok"}})


def _no_llm(**_kw):
    raise AssertionError("a turn from the atelier is decided without the classifier")


def _route(monkeypatch, http, message, page=_PAGE, nora_spoke=None):
    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", http)
    monkeypatch.setattr(R, "_asks_fast_answer", lambda *a, **k: False)
    return R.route_chat_message(
        message=message, session_chat_id="webhook:nora_chat:test", conversation_id="conv-atelier",
        thread_id=None, user_id="u", notifier_profile="default", idempotency_key="k",
        call_llm_fn=_no_llm, main_runtime=None, deliver_extra=dict(_EXTRA), chat_user="staff@example.test",
        page_context=page, nora_spoke=nora_spoke)


def test_the_atelier_is_read_from_the_page_context():
    assert R._atelier_of(_PAGE) == _ATELIER
    assert R._atelier_of({"atelier": {"space": "Commercial"}}) == {
        "space": "Commercial", "label": "Commercial", "scope": "user"}
    assert R._atelier_of({"route": "app/selling"}) is None
    assert R._atelier_of({"atelier": {"label": "Ventes"}}) is None
    assert R._atelier_of({"atelier": "Commercial"}) is None
    assert R._atelier_of(None) is None


def test_words_that_name_no_space_go_to_nora_with_the_atelier(monkeypatch):
    message = "Masque les abonnements, je ne m'en sers pas"
    assert not R._is_space_request(message)
    http = _nora({"ok": True, "proposed": True, "space": "Commercial", "ack": _PROPOSED})
    decision = _route(monkeypatch, http, message)
    assert decision["routed"] is True and decision["ack"] == _PROPOSED
    (url, body), (ack_url, ack_body) = http.calls
    assert url.endswith("nora.api.v2.space_route.route_space")
    assert body["atelier"] == _ATELIER and body["message"] == message
    assert ack_url == _CB and ack_body == {"conversation_id": "conv-atelier", "text": _PROPOSED}


def test_what_nora_does_not_read_reaches_the_orchestrator_with_the_atelier_instruction(monkeypatch):
    http = _nora({"ok": False, "error": "not read"})
    decision = _route(monkeypatch, http, "Fais-moi un espace plus clair pour mes rendez-vous")
    hint = decision["agent_hint"]
    assert decision["routed"] is False and hint == R._ATELIER_HINT.format(**_ATELIER)
    assert "space=Commercial, scope=user" in hint and "« Ventes »" in hint
    assert "WITHOUT confirmed" in hint and "do NOT search the wiki" in hint
    assert "A space just made has no list yet" in hint and "a word at a time" in hint


def test_a_yes_in_a_fresh_atelier_thread_is_the_ateliers_not_the_canned_answer(monkeypatch):
    keep = "Gardez-le directement dans l'atelier : …"
    http = _nora({"ok": True, "space": "Commercial", "ack": keep})
    decision = _route(monkeypatch, http, "Oui, vas-y.", nora_spoke=False)
    assert decision["ack"] == keep and http.calls[0][1]["atelier"] == _ATELIER


def test_a_note_written_from_the_atelier_stays_a_note(monkeypatch):
    http = _Http({"notes.route_note": {"message": {"ok": True, "ack": "C'est noté dans vos notes."}},
                  "hermes_callback.deliver": {"message": "ok"}})
    decision = _route(monkeypatch, http, "Crée une note : revoir l'espace Commercial avec l'équipe lundi")
    assert all("space_route" not in url for url, _body in http.calls)
    assert decision.get("agent_hint") not in (R._SPACE_HINT, R._ATELIER_HINT.format(**_ATELIER))


def test_outside_the_atelier_the_same_words_are_routed_as_before(monkeypatch):
    asked = []
    monkeypatch.setattr(R, "classify", lambda message, **kw: asked.append(message) or "DIRECT")
    http = _nora({"ok": True, "proposed": True, "ack": _PROPOSED})
    decision = _route(monkeypatch, http, "Masque les abonnements, je ne m'en sers pas", page={"route": "app/home"})
    assert asked == ["Masque les abonnements, je ne m'en sers pas"]
    assert all("space_route" not in url for url, _body in http.calls)
    assert decision.get("agent_hint") is None
