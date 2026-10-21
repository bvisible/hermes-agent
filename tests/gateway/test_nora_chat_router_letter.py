# //// Neoffice — added file (no upstream equivalent): a letter to post goes to the letter tool, never to the dunning
# //// rule (21.10, tranche 1 of the letters NORA writes in NeoWrite).
"""« Prépare une lettre pour notre assurance : … » goes to the support pole, which holds nora_letter_create.

Before, a letter matched no rule: the classifier, which has no letter class, decided; and « une lettre de relance pour
l'énergie », a letter to a supplier, reached compta through the dunning rule's « relance ». A customer payment
reminder (« relance » or « rappel » next to an invoice, a payment, a debtor, a customer) stays compta's. From an
open editor the letter is the orchestrator's turn, with the instruction to call the tool. All names are invented.
"""
import io
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway import nora_chat_router as R

VECTORS = json.loads((Path(__file__).parent / "letter_request_vectors.json").read_text(encoding="utf-8"))
_CB = "https://erp.example.test/api/method/nora.api.v2.hermes_callback.deliver"
_EXTRA = {"callback_url": _CB, "callback_token": "tok", "conversation_id": "conv-letter"}
_EDITOR_PAGE = {"surface": "editor", "app": "word", "document": {"name": "Brouillon.docx", "pages": 1}}


@pytest.fixture(autouse=True)
def fresh_state():
    for d in (R._LAST_ROUTE, R._PENDING_SPACE, R._PENDING_NOTE, R._CONV_HISTORY, R._SPACE_ASKED):
        d.clear()
    yield
    for d in (R._LAST_ROUTE, R._PENDING_SPACE, R._PENDING_NOTE, R._CONV_HISTORY, R._SPACE_ASKED):
        d.clear()


@pytest.mark.parametrize("message", VECTORS["letters"])
def test_a_letter_to_post_is_recognised_and_goes_to_support(message):
    assert R._is_letter_request(message), message
    assert R._fast_path(message, prior=None) == "support", message


@pytest.mark.parametrize("message", VECTORS["dunning"])
def test_an_explicit_payment_reminder_stays_the_dunning(message):
    assert not R._is_letter_request(message), message
    # The French ones by the dunning rule; an English one is the classifier's, as before (no English dunning rule).
    assert R._fast_path(message, prior=None) in ("compta", None), message


@pytest.mark.parametrize("message", VECTORS["not_letters"])
def test_these_are_not_letters(message):
    assert not R._is_letter_request(message), message


@pytest.mark.parametrize("message", VECTORS["capability"])
def test_a_question_about_letters_without_a_recipient_stays_a_capability_question(message):
    assert R._fast_path(message, prior=None) == "DIRECT", message


def test_the_supplier_letter_of_the_owners_example_escapes_the_dunning_rule():
    message = "Génère-moi une lettre de relance pour l'énergie"
    dunning = next(rx for rx, pole in R._FAST_PATH_RULES if pole == "compta" and "relanc" in rx.pattern)
    assert dunning.search(message), "the dunning rule alone would take it"
    assert R._keyword_pole(message) == "support"


def test_a_letter_beats_the_job_rules_and_the_employee_record():
    assert R._keyword_pole("Écris une lettre au client pour fixer la visite du chantier") == "support"


def test_a_letter_after_another_pole_is_still_a_letter():
    prior = {"msg": "Combien de factures ouvertes ?", "pole": "compta"}
    assert R._fast_path("Fais une lettre à la gérance pour la fuite d'eau", prior) == "support"


def test_a_go_ahead_after_a_letter_stays_on_support():
    prior = {"msg": "Prépare une lettre à la gérance", "pole": "support"}
    assert R._fast_path("oui, fais la lettre", prior) == "support"


def test_the_hint_names_only_nora_tools():
    # det/67 holds every nora_* tool named in a *_HINT to the orchestrator's own server.
    import re

    assert set(re.findall(r"\bnora_[a-z_]+", R._LETTER_HINT)) == {"nora_letter_create"}


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
    kanban_db = types.SimpleNamespace(create_task=lambda conn, **kw: created.append(kw) or "t_letter")
    connect = types.SimpleNamespace(connect=lambda board=None: SimpleNamespace(close=lambda: None))
    notify = types.SimpleNamespace(add_notify_sub=lambda *a, **kw: None)
    pkg = sys.modules.get("hermes_cli") or types.ModuleType("hermes_cli")
    monkeypatch.setitem(sys.modules, "hermes_cli", pkg)
    for name, mod in (("kanban_db", kanban_db), ("kanban_db_connect", connect), ("kanban_db_notify", notify)):
        monkeypatch.setattr(pkg, name, mod, raising=False)
        monkeypatch.setitem(sys.modules, f"hermes_cli.{name}", mod)
    return created


def _route(monkeypatch, message, page_context=None, fast_answer=None):
    import urllib.request

    asked = []
    monkeypatch.setattr(urllib.request, "urlopen", _Http())
    monkeypatch.setattr(R, "_asks_fast_answer", lambda *a, **k: True)
    monkeypatch.setattr(R, "_fast_answer", lambda *a, **k: asked.append(a) or fast_answer)
    decision = R.route_chat_message(
        message=message, session_chat_id="webhook:nora_chat:test", conversation_id="conv-letter",
        thread_id=None, user_id="u", notifier_profile="default", idempotency_key="k",
        call_llm_fn=lambda **_kw: pytest.fail("a letter is decided without the classifier"), main_runtime=None,
        deliver_extra=dict(_EXTRA), chat_user="staff@example.test", language="fr", page_context=page_context,
        nora_spoke=True)
    return decision, asked


def test_the_desk_letter_is_a_support_task_that_names_the_tool(monkeypatch):
    created = _fake_kanban(monkeypatch)
    message = VECTORS["letters"][0]
    decision, asked = _route(monkeypatch, message, fast_answer="a read the fast answer must never give")
    assert decision["routed"] is True and decision["category"] == "support"
    assert asked == [], "a letter is a gesture: the fast answer is not asked"
    body = created[-1]["body"]
    assert created[-1]["assignee"] == "support"
    assert body.startswith(R._LETTER_HINT) and message in body


def test_a_letter_on_a_job_page_stays_a_letter(monkeypatch):
    created = _fake_kanban(monkeypatch)
    decision, _asked = _route(monkeypatch, "Écris une lettre à la régie Fictive pour la fuite",
                              page_context={"job": {"project": "PROJ-0001"}, "project": "PROJ-0001"})
    assert decision["category"] == "support" and created[-1]["assignee"] == "support"


def test_a_letter_from_an_open_editor_is_the_orchestrators_with_the_tool(monkeypatch):
    created = _fake_kanban(monkeypatch)
    decision, asked = _route(monkeypatch, "Fais une lettre à la gérance pour le problème d'eau", page_context=_EDITOR_PAGE)
    assert created == [] and asked == []
    assert decision["routed"] is False and decision["category"] == "DIRECT"
    hint = decision["agent_hint"]
    assert R._EDITOR_HINT.format(app="word", name="Brouillon.docx") in hint and R._LETTER_HINT in hint


def test_a_document_edit_from_the_editor_gets_no_letter_hint(monkeypatch):
    _fake_kanban(monkeypatch)
    decision, _asked = _route(monkeypatch, "Mets le titre en gras", page_context=_EDITOR_PAGE)
    assert R._LETTER_HINT not in (decision.get("agent_hint") or "")


def test_a_payment_reminder_letter_is_still_routed_to_compta(monkeypatch):
    created = _fake_kanban(monkeypatch)
    decision, _asked = _route(monkeypatch, VECTORS["dunning"][0])
    assert decision["category"] == "compta" and R._LETTER_HINT not in created[-1]["body"]
