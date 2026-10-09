# //// Neoffice — added file (no upstream equivalent): sales documents and charts asked for in English,
# //// German and Italian reach their pole as the French words do (08.10, capability bench).
"""The keyword rules are written in French; the fleet's people also write in English, German, Italian.

« How many open quotes does <a client> have, and what is their total amount? » matched no rule, the
classifier answered 'direct', and the orchestrator handed it on without the person's language: the
answer came back in French. These sentences now reach ventes (or analyse, for a chart) without the
classifier; a job named in the message and a short question about what NORA can do still go to it.
"""
import io
import json

import pytest

from gateway import nora_chat_router as R


@pytest.mark.parametrize("message", (
    # the bench's own sentence, then the same question in the other languages
    "How many open quotes does Martin SA have, and what is their total amount?",
    "Wie viele offene Angebote hat Martin SA, und wie hoch ist der Gesamtbetrag?",
    "Quanti preventivi aperti ha Martin SA, e per quale importo totale?",
    "Show me the last quotation for Martin SA",
    "Create a quote for Martin SA with three hoses",
    "Can you tell me how many quotes we sent this month?",
    "List the open sales orders",
    "Which customer orders are late?",
    "Erstelle eine Offerte für Martin SA",
    "Zeig mir die offenen Kundenaufträge",
    "Kannst du mir die Angebote von Martin SA zeigen?",
    "Fai un preventivo per Martin SA con tre flessibili",
    "Quali ordini dei clienti sono in ritardo?",
    "Mandami l'offerta per Martin SA",
))
def test_a_sales_document_in_another_language_reaches_ventes(message):
    assert R._fast_path(message, prior=None) == "ventes"


@pytest.mark.parametrize("message", (
    "Show the open quotes as a chart",
    "Make a dashboard of this year's sales orders",
    "Zeig mir die Angebote als Balkendiagramm",
    "Mostrami un grafico dei preventivi",
    "Plot a graph of revenue by month",
))
def test_a_chart_in_another_language_reaches_analyse(message):
    assert R._fast_path(message, prior=None) == "analyse"


@pytest.mark.parametrize("message", (
    # a job named in the message: projet's, the classifier knows it
    "What is the quote for the construction site in Lausanne?",
    "Wie hoch ist das Angebot für die Baustelle Müller?",
    "Il preventivo del cantiere di Lugano è pronto?",
    # a short question about what NORA can do, the case _CAPABILITY_RE answers in French
    "Can you make quotes?",
    "Could you handle sales orders for us?",
    "Kannst du Angebote erstellen?",
    "Puoi fare preventivi?",
    # the ledger is not a chart
    "Show me the chart of accounts",
    # a graphics card is an article, not a chart
    "Wie viele Grafikkarten haben wir auf Lager?",
))
def test_these_stay_with_the_classifier(message):
    assert R._fast_path(message, prior=None) is None


@pytest.mark.parametrize("message", (
    "Pubblica un'offerta di lavoro per un contabile",
    "Welche Stellenangebote sind online?",
))
def test_a_job_offer_is_still_recruitment(message):
    assert R._fast_path(message, prior=None) == "rh"


@pytest.mark.parametrize("message, pole", (
    ("Combien de devis en cours a Martin SA, et pour quel montant au total ?", "ventes"),
    ("Montre-moi le chiffre d'affaires en graphique", "analyse"),
    ("Quel est le statut du chantier PROJ-0091 ?", "projet"),
))
def test_the_french_rules_are_unchanged(message, pole):
    assert R._fast_path(message, prior=None) == pole


def test_the_classifier_is_told_that_the_language_changes_nothing():
    assert "La langue du message ne change rien" in R._CLASSIFIER_SYSTEM


# ── the person's language, remembered for a task the orchestrator creates ─────────────

class _Http:
    """Stands in for urllib.request.urlopen: every call answers an empty result."""

    def __init__(self):
        self.calls = []

    def __call__(self, req, timeout=None):
        self.calls.append(req.full_url)
        return _Resp()


class _Resp:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return io.BytesIO(json.dumps({"message": None}).encode()).getvalue()


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    monkeypatch.setattr(R, "_CHAT_LANGUAGE", {})
    R._LAST_ROUTE.clear()
    R._CONV_HISTORY.clear()
    yield
    R._LAST_ROUTE.clear()
    R._CONV_HISTORY.clear()


def test_a_message_handed_on_leaves_its_language_for_the_orchestrators_task(monkeypatch):
    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", _Http())
    monkeypatch.setattr(R, "classify", lambda *a, **k: "DIRECT")
    chat = "webhook:nora_chat:user:someone@example.test"
    decision = R.route_chat_message(
        message="Tell me something about the weather, please", session_chat_id=chat,
        conversation_id="conv-lang", thread_id=None, user_id="u", notifier_profile="default",
        idempotency_key="k", call_llm_fn=lambda **_kw: "direct", main_runtime=None,
        deliver_extra={"callback_url": "https://erp.example.test/cb", "conversation_id": "conv-lang"},
        chat_user="someone@example.test", language="en")
    assert decision["routed"] is False
    assert R.chat_reply_directive(chat) == R.worker_reply_directive("en")
    assert "Reply to the user in English" in R.chat_reply_directive(chat)


def test_a_chat_the_router_never_saw_has_no_directive():
    assert R.chat_reply_directive("webhook:nora_chat:user:nobody@example.test") is None
    assert R.chat_reply_directive("") is None


@pytest.mark.parametrize("language, name", (("en", "English"), ("de-CH", "German"), ("it", "Italian"),
                                            ("fr", "French"), (None, "French"), ("xx", "French")))
def test_the_directive_names_the_language_and_keeps_the_partner_guard(language, name):
    directive = R.worker_reply_directive(language)
    assert directive.startswith(f"{R.REPLY_DIRECTIVE_MARK}{name}. Do not reply in any other language.")
    assert "do NOT pick one yourself" in directive and directive.endswith("and STOP.]")
    # //// Neoffice — 09.10: amounts with their currency, the ERP's words in the reply's language
    assert "every amount with its currency (CHF)" in directive and "« Brouillon »" in directive
    # //// Neoffice — 09.10: the Swiss number format, as the fast path writes it
    assert "1'234.50 CHF" in directive
