# //// Neoffice — added file (no upstream equivalent): a one-off reminder goes to NORA
# //// herself, who holds nora_reminder_create; a payment reminder stays compta.
"""« Rappelle-moi demain à 10 h » is routed to NORA, not read as a payment reminder."""
import pytest

from gateway.nora_chat_router import _fast_path


@pytest.mark.parametrize("message", (
    "Rappelle-moi demain à 10h d'appeler Dupont",
    "Mets-moi un rappel lundi à 8 h pour la TVA",
    "rappelle-moi dans 2 heures de relancer le client Martin",
    "Fais-moi penser cet après-midi à envoyer le devis",
    "Crée un rappel le 3 octobre : renouveler l'assurance",
    "Erinnere mich morgen um 9 Uhr an die Sitzung",
    "Remind me tomorrow at 10:00 to call the bank",
    "Erinnere mich übermorgen an den Zahnarzt",
    "Ricordami domani alle 15 di chiamare il notaio",
    "Remind me in 2 hours to call back the supplier",
    "Remind me on Friday at 5pm to send the report",
))
def test_a_one_off_reminder_goes_to_nora(message):
    assert _fast_path(message, prior=None) == "DIRECT"


def test_it_wins_over_the_conversation_context():
    assert _fast_path("Rappelle-moi demain à 9h de payer Sunrise", prior={"pole": "compta"}) == "DIRECT"


@pytest.mark.parametrize("message, expected", (
    ("Crée un rappel de paiement pour Dupont demain", "compta"),
    ("Envoie un rappel de facture à Martin", "compta"),
))
def test_a_payment_reminder_stays_compta(message, expected):
    assert _fast_path(message, prior=None) == expected


@pytest.mark.parametrize("message", (
    "Rappelle-moi tous les lundis à 8h de faire la TVA",   # repeated → the classifier's 'recurrent'
    "Rappelle-moi combien on a facturé en août",           # « tell me again », no moment
    # A habit in another language was a one-off « next Monday » while _RECUR_RE was French.
    "Remind me every Monday at 10:00 to call the bank",
    "Erinnere mich jeden Freitag um 16 Uhr an den Wochenbericht",
    "Ricordami ogni giorno alle 9 di controllare la cassa",
    "Remind me daily at 9 to back up the laptop",
    "Erinnere mich montags an die Kassenabrechnung",
))
def test_not_a_one_off_reminder(message):
    assert _fast_path(message, prior=None) != "DIRECT"


def test_a_short_go_ahead_after_a_compta_proposal_stays_compta():
    assert _fast_path("ok crée le rappel", prior={"pole": "compta"}) == "compta"


def _route(message):
    from gateway.nora_chat_router import route_chat_message

    def no_llm(**_kw):
        raise AssertionError("a one-off reminder is decided without the classifier")

    return route_chat_message(
        message=message, session_chat_id="webhook:nora_chat:test", conversation_id=None, thread_id=None,
        user_id="u", notifier_profile="default", idempotency_key="k", call_llm_fn=no_llm, main_runtime=None)


def test_the_orchestrator_is_told_to_set_the_reminder_itself():
    decision = _route("Rappelle-moi demain à 10h d'appeler Dupont")
    assert decision["routed"] is False and decision["category"] == "DIRECT"
    hint = decision["agent_hint"]
    assert "nora_reminder_create" in hint and "do NOT call kanban_create" in hint


def test_no_instruction_rides_with_an_ordinary_direct_message():
    decision = _route("Merci beaucoup !")
    assert not decision.get("agent_hint")


# ── the reminder is SET IN CODE by nora (task_router.route_reminder) ─────────────────
# The hint above was not enough on the dev instance: the orchestrator delegated, then
# repeated its own earlier « C'est noté » from memory, and no reminder was ever written.

import io
import json

_CB = "https://erp.example.test/api/method/nora.api.v2.hermes_callback.deliver"
_EXTRA = {"callback_url": _CB, "callback_token": "tok", "conversation_id": "conv-1"}


class _Http:
    """Stands in for urllib.request.urlopen; records every POST and answers per URL."""

    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def __call__(self, req, timeout=None):
        body = json.loads(req.data.decode())
        self.calls.append((req.full_url, body, dict(req.header_items())))
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


def _route_with(http, monkeypatch, message="Rappelle-moi demain à 10h d'appeler Dupont", extra=_EXTRA):
    import urllib.request
    from gateway.nora_chat_router import route_chat_message

    monkeypatch.setattr(urllib.request, "urlopen", http)

    def no_llm(**_kw):
        raise AssertionError("a one-off reminder is decided without the classifier")

    return route_chat_message(
        message=message, session_chat_id="webhook:nora_chat:test", conversation_id="conv-1",
        thread_id=None, user_id="u", notifier_profile="default", idempotency_key="k",
        call_llm_fn=no_llm, main_runtime=None, deliver_extra=extra, chat_user="staff@example.test")


def test_the_reminder_is_written_by_nora_and_acknowledged(monkeypatch):
    ack = "C'est noté : je vous le rappelle vendredi 25 septembre 2026 à 10:00 — « appeler Dupont »."
    http = _Http({"task_router.route_reminder": {"message": {"ok": True, "reminder": "R-0001", "ack": ack}},
                  "hermes_callback.deliver": {"message": "ok"}})

    decision = _route_with(http, monkeypatch)

    assert decision["routed"] is True and decision["reminder"] == "R-0001" and decision["ack"] == ack
    # Two POSTs, no more: the read-only fast-answer engine is not asked about an action.
    (url, body, headers), (ack_url, ack_body, _h) = http.calls
    assert url.endswith("nora.api.v2.task_router.route_reminder")
    assert body["user"] == "staff@example.test" and "Dupont" in body["message"]
    assert headers.get("X-hermes-token") == "tok"
    assert ack_url == _CB and ack_body == {"conversation_id": "conv-1", "text": ack}


@pytest.mark.parametrize("answer", (
    {"message": {"ok": False, "error": "no moment to come"}},   # declined by nora
    OSError("connection refused"),                              # nora unreachable
))
def test_a_declined_or_failed_reminder_falls_back_to_the_agent_with_the_instruction(monkeypatch, answer):
    http = _Http({"task_router.route_reminder": answer})

    decision = _route_with(http, monkeypatch)

    assert decision["routed"] is False and "nora_reminder_create" in decision["agent_hint"]
    assert len(http.calls) == 1, "no acknowledgement is sent for a reminder that was not written"


def test_without_a_desk_callback_nothing_is_posted(monkeypatch):
    http = _Http({})

    decision = _route_with(http, monkeypatch, extra={})

    assert decision["routed"] is False and decision["agent_hint"] and http.calls == []


def test_a_recurring_request_reads_the_answer_inside_frappes_envelope(monkeypatch):
    """Frappe answers {"message": {...}}; reading "ok" on the envelope declined every
    recurring request (dev-instance log: « recurrent declined (None) »)."""
    import urllib.request
    from gateway.nora_chat_router import _route_recurrent

    http = _Http({"task_router.route_recurrent": {"message": {"ok": True, "task_id": "NST-1", "ack": "C'est noté."}},
                  "hermes_callback.deliver": {"message": "ok"}})
    monkeypatch.setattr(urllib.request, "urlopen", http)

    decision = _route_recurrent("Relève mes mails tous les matins à 8h", "staff@example.test", _EXTRA)

    assert decision["routed"] is True and decision["task_id"] == "NST-1"
