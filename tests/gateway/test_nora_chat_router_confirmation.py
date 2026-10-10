# //// Neoffice — added file (no upstream equivalent): a « oui » to what NORA offered is carried out by nora (10.10).
"""« Oui, valide-la » and « Oui, envoyez » cost a worker turn each, for an action NORA's last reply had already
named. A short go-ahead after a pole's proposal is first handed to nora, which carries the action out in code and
answers; when nora declines, the message is routed as before.
"""
import io
import json

import pytest

from gateway import nora_chat_router as R

_CB = "https://erp.example.test/api/method/nora.api.v2.hermes_callback.deliver"
_EXTRA = {"callback_url": _CB, "callback_token": "tok", "conversation_id": "conv-c"}
_ACK = "C'est validé : la commande d'achat PO-1 (Fournisseur, 108.10 CHF). Voulez-vous que je l'envoie au fournisseur ?"


class _Http:
    """Stands in for urllib.request.urlopen; records every POST and answers per URL."""

    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def __call__(self, req, timeout=None):
        self.calls.append((req.full_url, json.loads(req.data.decode()), dict(req.header_items())))
        answer = next((a for suffix, a in self.answers.items() if req.full_url.endswith(suffix)), None)
        if isinstance(answer, Exception):
            raise answer
        return _Resp(io.BytesIO(json.dumps(answer or {}).encode()))

    def urls(self, suffix):
        return [url for url, _body, _headers in self.calls if url.endswith(suffix)]


class _Resp:
    def __init__(self, buf):
        self._buf, self.status = buf, 200

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return self._buf.getvalue()


class _Routed(Exception):
    """The message went on to the classifier: routed as before."""


def _route(http, monkeypatch, message, prior_pole="ventes"):
    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", http)
    monkeypatch.setattr(R, "classify", lambda *a, **k: (_ for _ in ()).throw(_Routed()))
    R._LAST_ROUTE.pop("conv-c", None)
    R._CONV_HISTORY.pop("conv-c", None)
    if prior_pole:
        R._LAST_ROUTE["conv-c"] = {"msg": "Commande-en 10", "pole": prior_pole, "title": "Commande-en 10", "tasks": False}

    def no_llm(**_kw):
        raise AssertionError("no model call before nora was asked")

    return R.route_chat_message(
        message=message, session_chat_id="webhook:nora_chat:test", conversation_id="conv-c", thread_id=None,
        user_id="u", notifier_profile="default", idempotency_key="k", call_llm_fn=no_llm, main_runtime=None,
        deliver_extra=_EXTRA, chat_user="staff@example.test", language="fr")


def test_a_yes_to_an_offer_is_carried_out_by_nora_and_answered_at_once(monkeypatch):
    http = _Http({"confirmations.route_confirmation": {"message": {"ok": True, "action": "submit", "ack": _ACK}},
                  "hermes_callback.deliver": {"message": "ok"}})

    decision = _route(http, monkeypatch, "Oui, valide-la.")

    assert decision["routed"] is True and decision["ack"] == _ACK and decision["confirmed"] == "submit"
    (url, body, headers), (ack_url, ack_body, _h) = http.calls
    assert url.endswith("nora.api.v2.confirmations.route_confirmation")
    assert body == {"user": "staff@example.test", "message": "Oui, valide-la.", "conversation_id": "conv-c",
                    "language": "fr"}
    assert headers.get("X-hermes-token") == "tok"
    assert ack_url == _CB and ack_body == {"conversation_id": "conv-c", "text": _ACK}
    # the conversation goes on: the pole stays, the film holds the yes and the answer for the next worker
    assert R._LAST_ROUTE["conv-c"]["pole"] == "ventes"
    assert R._CONV_HISTORY["conv-c"][-2:] == ["User: Oui, valide-la.", "NORA: " + _ACK]


@pytest.mark.parametrize("answer", (
    {"message": {"ok": False, "reason": "nothing or two things offered"}},  # declined by nora
    OSError("connection refused"),                                       # nora unreachable
))
def test_a_declined_or_failed_confirmation_is_routed_as_before(monkeypatch, answer):
    http = _Http({"confirmations.route_confirmation": answer})

    with pytest.raises(_Routed):
        _route(http, monkeypatch, "Oui, envoie-la au fournisseur.")

    assert len(http.urls("confirmations.route_confirmation")) == 1
    assert not http.urls("hermes_callback.deliver"), "nothing is said for an action that was not carried out"


@pytest.mark.parametrize("message, prior_pole", (
    ("Oui", None),                                          # no pole proposed anything in this conversation
    ("Commande 10 siphons de lavabo", "ventes"),             # a request, not a yes
    ("Ok. Est-ce qu'il y a des rappels à faire ?", "ventes"),  # a question, not a yes
))
def test_nora_is_not_asked_without_a_go_ahead_to_a_pole(monkeypatch, message, prior_pole):
    http = _Http({})

    with pytest.raises(_Routed):
        _route(http, monkeypatch, message, prior_pole=prior_pole)

    assert not http.urls("confirmations.route_confirmation")
