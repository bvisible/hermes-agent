# //// Neoffice — added file (no upstream equivalent): a name typed alone is never left to DIRECT (09.10).
"""« Atelier Démo SA », typed alone in the chat, reaches the pole that holds its record, never the orchestrator.

Seen on the development instance: the classifier called the bare customer name 'direct'. On 04.10 the
orchestrator answered « Je n'ai pas réussi à traiter votre demande » after 11 s; on 09.10 it took 22.7 s to hand
it to the job pole, whose answer came at 71.6 s. The person wants the record of what they named. A 'direct'
verdict on a bare name is asked again with 'direct' excluded; ventes, which holds the customers and suppliers,
when the classifier still cannot choose.
"""
import io
import json
import sys
import types
from types import SimpleNamespace

import pytest

from gateway import nora_chat_router as R

_CB = "https://erp.example.test/api/method/nora.api.v2.hermes_callback.deliver"
_EXTRA = {"callback_url": _CB, "callback_token": "tok", "conversation_id": "conv-name"}


@pytest.fixture(autouse=True)
def fresh_state():
    for d in (R._LAST_ROUTE, R._PENDING_SPACE, R._PENDING_NOTE, R._CONV_HISTORY, R._SPACE_ASKED):
        d.clear()
    yield
    for d in (R._LAST_ROUTE, R._PENDING_SPACE, R._PENDING_NOTE, R._CONV_HISTORY, R._SPACE_ASKED):
        d.clear()


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
    kanban_db = types.SimpleNamespace(create_task=lambda conn, **kw: created.append(kw) or "t_name")
    connect = types.SimpleNamespace(connect=lambda board=None: SimpleNamespace(close=lambda: None))
    notify = types.SimpleNamespace(add_notify_sub=lambda *a, **kw: None)
    pkg = sys.modules.get("hermes_cli") or types.ModuleType("hermes_cli")
    monkeypatch.setitem(sys.modules, "hermes_cli", pkg)
    for name, mod in (("kanban_db", kanban_db), ("kanban_db_connect", connect), ("kanban_db_notify", notify)):
        monkeypatch.setattr(pkg, name, mod, raising=False)
        monkeypatch.setitem(sys.modules, f"hermes_cli.{name}", mod)
    return created


def _route(monkeypatch, message, verdicts):
    """Route *message* with the classifier answering *verdicts* in turn; returns (decision, calls)."""
    import urllib.request

    calls = []

    def fake_classify(message, **kw):
        calls.append(kw)
        return verdicts[len(calls) - 1]

    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda req, timeout=None: _Resp(io.BytesIO(json.dumps({"message": "ok"}).encode())),
    )
    monkeypatch.setattr(R, "_asks_fast_answer", lambda *a, **k: False)
    monkeypatch.setattr(R, "classify", fake_classify)
    decision = R.route_chat_message(
        message=message, session_chat_id="webhook:nora_chat:test", conversation_id="conv-name",
        thread_id=None, user_id="u", notifier_profile="default", idempotency_key="k",
        call_llm_fn=lambda **_kw: None, main_runtime=None, deliver_extra=dict(_EXTRA),
        chat_user="staff@example.test", language="fr", nora_spoke=True,
    )
    return decision, calls


def test_a_direct_verdict_on_a_bare_name_is_asked_again(monkeypatch):
    created = _fake_kanban(monkeypatch)
    decision, calls = _route(monkeypatch, "Atelier Démo SA", ["DIRECT", "ventes"])
    assert decision["routed"] is True and decision["category"] == "ventes"
    assert created[-1]["assignee"] == "ventes"
    assert [kw.get("exclude_direct", False) for kw in calls] == [False, True]
    assert "nom" in (calls[1].get("exclude_reason") or "")


def test_ventes_takes_a_bare_name_the_classifier_still_calls_direct(monkeypatch):
    created = _fake_kanban(monkeypatch)
    decision, _ = _route(monkeypatch, "Boulangerie du Lac", ["DIRECT", "DIRECT"])
    assert decision["category"] == "ventes" and created[-1]["assignee"] == "ventes"


def test_a_single_ambiguous_word_stays_direct(monkeypatch):
    created = _fake_kanban(monkeypatch)
    decision, calls = _route(monkeypatch, "Alltron", ["DIRECT"])
    assert decision["category"] == "DIRECT" and created == [] and len(calls) == 1


@pytest.mark.parametrize("message, bare", [
    ("Atelier Démo SA", True),
    ("Atelier Démo SA ?", True),
    ("Daniel Moret", True),
    ("Boulangerie du Lac", True),
    ("Quincaillerie du Banc Sàrl", True),
    ("Menuiserie de l'Etang S.A.", True),
    ("Jean-Pierre D'Amico", True),
    ("Alltron", False),                 # one word: a name, a product or anything else
    ("Merci Beaucoup", False),
    ("Bonne Journée", False),
    ("Joyeux Noël", False),
    ("Bonjour Nora", False),
    ("Test Test", False),
    ("C'est Daniel Moret", False),      # a sentence, not a name
    ("Daniel Moret a payé", False),
    ("Crée un devis", False),
    ("devis", False),
])
def test_what_is_a_bare_name(message, bare):
    assert R._is_bare_name(message) is bare
