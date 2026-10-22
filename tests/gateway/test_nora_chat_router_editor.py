# //// Neoffice — added file (no upstream equivalent): what the person writes from an open document in an editor
# //// is about that document, whatever its words (maintenance#1573).
"""« Mets le mot offre en gras, puis ajoute à la fin une ligne », written in Word, went to the Support pole.

The panel of the editors (Word, Excel, PowerPoint, PDF, in the Drive or in NeoDesk) sends every message with the page
context {surface: "editor", app, document: {name, …}}. The document tools are NORA's own (nora_doc_*, never a
pole's), so a turn from an editor is the orchestrator's, with the instruction to use them. The turns settled in code
(a note, a space, the scheduled tasks, a reminder, small talk) stay settled in code.
"""
import io
import json

import pytest

from gateway import nora_chat_router as R

_CB = "https://erp.example.test/api/method/nora.api.v2.hermes_callback.deliver"
_EXTRA = {"callback_url": _CB, "callback_token": "tok", "conversation_id": "conv-editor"}
_EDITOR = {"app": "word", "name": "Devis.docx"}
_PAGE = {"surface": "editor", "app": "word", "document": {"name": "Devis.docx", "pages": 1}}
_EDIT = "Mets le mot offre en gras, puis ajoute à la fin du document une ligne : Validité : 30 jours."


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


def _no_llm(**_kw):
    raise AssertionError("a turn from an editor is decided without the classifier")


def _route(monkeypatch, message, page=_PAGE, http=None, call_llm_fn=_no_llm, **kwargs):
    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", http or _Http({"hermes_callback.deliver": {"message": "ok"}}))
    monkeypatch.setattr(R, "_asks_fast_answer", lambda *a, **k: False)
    return R.route_chat_message(
        message=message, session_chat_id="webhook:nora_chat:test", conversation_id="conv-editor",
        thread_id=None, user_id="u", notifier_profile="default", idempotency_key="k",
        call_llm_fn=call_llm_fn, main_runtime=None, deliver_extra=dict(_EXTRA), chat_user="staff@example.test",
        page_context=page, **kwargs)


def test_the_editor_is_read_from_the_page_context():
    assert R._editor_of(_PAGE) == _EDITOR
    assert R._editor_of({"surface": "editor"}) == {"app": "word", "name": "an untitled document"}
    # The server reads the editor the same way (neoffice_theme.editor_actions.app_of): lower case, Word by default.
    assert R._editor_of({"surface": "editor", "app": " Sheet "})["app"] == "sheet"
    assert R._editor_of({"surface": "editor", "app": "sheet", "document": "Budget.xlsx"}) == {
        "app": "sheet", "name": "an untitled document"}
    assert R._editor_of({"surface": "quick_chat", "app": "word"}) is None
    assert R._editor_of({"route": "app/selling"}) is None
    assert R._editor_of(None) is None


def test_a_request_about_the_document_goes_to_the_orchestrator_with_the_document_tools(monkeypatch):
    monkeypatch.setattr(R, "classify", lambda *a, **k: pytest.fail("the classifier must not be asked"))
    decision = _route(monkeypatch, _EDIT)
    hint = decision["agent_hint"]
    assert decision["routed"] is False and decision["category"] == "DIRECT" and decision["task_id"] is None
    assert hint == R._editor_hint(_EDITOR)
    assert "« Devis.docx »" in hint and "word editor" in hint
    assert "nora_doc_observe FIRST" in hint and "nora_doc_commands" in hint
    # What the real agent got wrong on osiris (14.10): tool names as command ids, a line typed over a selection.
    assert 'where="end"' in hint and "not command ids" in hint and "typing replaces the selection" in hint
    assert "Do NOT call kanban_create for the document" in hint
    # What is still business is said to be, so the hint does not turn every turn of an editor into a document turn.
    assert "company's own data" in hint and hint.rstrip("]").endswith("kanban_create.")


def test_it_is_not_a_pole_task_whatever_the_words(monkeypatch):
    # Words the keyword rules send to a pole: a customer, an invoice, hours.
    for words in ("Ajoute la facture du client Dupont en bas du document",
                  "Corrige les heures de travail dans le deuxième tableau",
                  "Mets le titre en gras"):
        decision = _route(monkeypatch, words)
        assert decision["routed"] is False and decision["agent_hint"] == R._editor_hint(_EDITOR), words
        assert R._LAST_ROUTE["conv-editor"]["pole"] is None


def test_the_same_words_outside_an_editor_are_classified_as_before(monkeypatch):
    asked = []
    monkeypatch.setattr(R, "classify", lambda message, **kw: asked.append(message) or "support")
    http = _Http({"hermes_callback.deliver": {"message": "ok"}})
    decision = _route(monkeypatch, _EDIT, page={"route": "app/home"}, http=http)
    assert asked == [_EDIT] and decision["category"] == "support"
    assert R._EDITOR_HINT_HEAD.split("{")[0] not in json.dumps(decision)


def test_a_help_request_asked_from_an_editor_stays_with_the_document(monkeypatch):
    decision = _route(monkeypatch, "Comment je mets un titre en gras ?", help_request="ask")
    assert decision["category"] == "DIRECT" and decision["agent_hint"] == R._editor_hint(_EDITOR)


def test_a_capability_question_asked_from_an_editor_is_not_the_light_paths(monkeypatch):
    message = "Tu peux faire quoi dans ce document ?"
    assert R._CAPABILITY_RE.match(message)
    calls = []

    def light_path(**kw):  # what the light path would ask: one short answer, delivered as it is
        calls.append(kw.get("task"))
        raise AssertionError("the light path answers a capability question from the generic list")

    # The light path swallows its own failures and falls back to the agent, so a call is recorded, not raised.
    decision = _route(monkeypatch, message, call_llm_fn=light_path)
    assert calls == [] and decision["routed"] is False
    assert decision["agent_hint"] == R._editor_hint(_EDITOR)


def test_small_talk_from_an_editor_is_still_a_canned_answer(monkeypatch):
    decision = _route(monkeypatch, "Merci beaucoup !", language="fr")
    assert decision["routed"] is True and decision.get("fast") is True and decision["ack"]
    assert "agent_hint" not in decision or not decision["agent_hint"]


def test_a_note_written_from_an_editor_stays_a_note(monkeypatch):
    http = _Http({"notes.route_note": {"message": {"ok": True, "ack": "C'est noté dans vos notes."}},
                  "hermes_callback.deliver": {"message": "ok"}})
    decision = _route(monkeypatch, "Crée une note : relire le devis avec Anna lundi", http=http)
    assert decision["routed"] is True and decision["ack"] == "C'est noté dans vos notes."
    assert any(url.endswith("notes.route_note") for url, _body in http.calls)


def test_a_one_off_reminder_asked_from_an_editor_is_still_set_in_code(monkeypatch):
    http = _Http({"task_router.route_reminder": {"message": {"ok": True, "ack": "C'est noté : lundi à 9 h."}},
                  "hermes_callback.deliver": {"message": "ok"}})
    decision = _route(monkeypatch, "Rappelle-moi lundi à 9h de relire ce devis", http=http)
    assert decision["routed"] is True and decision["ack"] == "C'est noté : lundi à 9 h."
    assert any(url.endswith("route_reminder") for url, _body in http.calls)


def test_the_hint_closes_its_braces_for_any_name(monkeypatch):
    page = {"surface": "editor", "app": "pdf", "document": {"name": "Contrat {2026} — v2.pdf"}}
    decision = _route(monkeypatch, "Résume ce document", page=page)
    assert "« Contrat {2026} — v2.pdf »" in decision["agent_hint"] and "pdf editor" in decision["agent_hint"]


# //// Neoffice — one instruction per editor (22.10, maintenance#1690): in NeoGrille the real agent said it could not go
# //// to B1; NeoPrés and NeoPDF were never told their recipes. The SOUL is full: the recipes ride with the turn.
_PAGES = {
    "word": {"surface": "editor", "app": "word", "document": {"name": "Devis.docx"}},
    "sheet": {"surface": "editor", "app": "sheet", "document": {"name": "Ventes.xlsx"}},
    "deck": {"surface": "editor", "app": "deck", "document": {"name": "Offre.pptx", "pages": 3}},
    "pdf": {"surface": "editor", "app": "pdf", "document": {"name": "Formulaire.pdf", "pages": 2}},
}


@pytest.mark.parametrize("app", sorted(_PAGES))
def test_each_editor_gets_its_own_recipes_with_the_common_rules(monkeypatch, app):
    page = _PAGES[app]
    decision = _route(monkeypatch, "Fais-le dans le document, s'il te plaît", page=page)
    hint = decision["agent_hint"]
    assert decision["routed"] is False and decision["category"] == "DIRECT"
    assert hint == R._editor_hint({"app": app, "name": page["document"]["name"]})
    assert f"in the {app} editor" in hint and f"« {page['document']['name']} »" in hint
    # The rules every editor keeps.
    assert "nora_doc_observe FIRST" in hint and "`how_to`" in hint and "not command ids" in hint
    assert "Do NOT call kanban_create for the document" in hint and hint.endswith("kanban_create.]")
    assert R._EDITOR_HINTS[app].replace("{{", "{").replace("}}", "}") in hint


def test_the_spreadsheet_is_worked_by_cell_address(monkeypatch):
    hint = _route(monkeypatch, "Écris Total dans A1 et 42 dans B1", page=_PAGES["sheet"])["agent_hint"]
    assert "by its ADDRESS" in hint and 'nora_doc_select_text("B4")' in hint
    assert 'select "A1", then type Total, a tab, 42' in hint
    assert 'where="end"' in hint and '"insert.chart"' in hint and '"range": "A1:B13"' in hint
    assert "typing replaces the selection" not in hint, "Word's rule is not the sheet's"


def test_rows_added_under_a_charted_table_are_put_in_its_chart(monkeypatch):
    hint = _route(monkeypatch, "Ajoute mai et juin", page=_PAGES["sheet"])["agent_hint"]
    assert 'nora_doc_execute("chart.selectData", {"chart": ' in hint and '"range": "Sheet1!A1:B7"' in hint
    assert "the sheet's last chart" in hint and "colours set on a series are lost" in hint
    assert "chart.selectData" not in "".join(R._editor_hint({"app": a, "name": "x"}) for a in ("word", "deck", "pdf"))


def test_a_presentation_takes_several_slides_in_one_call(monkeypatch):
    hint = _route(monkeypatch, "Ajoute trois diapositives sur nos tarifs", page=_PAGES["deck"])["agent_hint"]
    assert 'nora_doc_type_text(an outline, where="end")' in hint and 'starting with "- "' in hint
    assert "never one call per slide" in hint and '"slide.new"' in hint


def test_a_pdf_form_is_listed_then_filled_and_redaction_waits_for_a_yes(monkeypatch):
    hint = _route(monkeypatch, "Remplis ce formulaire", page=_PAGES["pdf"])["agent_hint"]
    assert '"form_fields"' in hint and 'ONE nora_doc_execute("form_fill"' in hint and "never invent" in hint
    assert "redact_apply" in hint and "only after the person said yes" in hint and "signing is theirs" in hint
    assert 'where="end"' not in hint


def test_no_editor_carries_another_editor_s_recipes():
    hints = {app: R._editor_hint({"app": app, "name": "x"}) for app in _PAGES}
    assert "form_fill" not in hints["word"] + hints["sheet"] + hints["deck"]
    assert "by its ADDRESS" not in hints["word"] + hints["deck"] + hints["pdf"]
    assert "outline" not in hints["word"] + hints["sheet"] + hints["pdf"]


def test_an_editor_the_router_does_not_know_still_gets_the_common_rules(monkeypatch):
    page = {"surface": "editor", "app": "mail", "document": {"name": "Réponse"}}
    hint = _route(monkeypatch, "Corrige les fautes", page=page)["agent_hint"]
    assert hint == R._editor_hint({"app": "mail", "name": "Réponse"})
    assert "nora_doc_observe FIRST" in hint and R._EDITOR_HINT_ANY in hint and hint.endswith("kanban_create.]")
    for recipes in R._EDITOR_HINTS.values():
        assert recipes.replace("{{", "{").replace("}}", "}") not in hint


def test_the_hints_carry_no_unfilled_brace():
    for app in [*_PAGES, "mail"]:
        hint = R._editor_hint({"app": app, "name": "Plan {Q3}"})
        assert "« Plan {Q3} »" in hint
        assert "{app}" not in hint and "{name}" not in hint and "{{" not in hint
