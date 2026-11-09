# //// Neoffice — added file (no upstream equivalent): a desk's question is asked again after a question about it (#2376).
"""« Qu'est-ce que je gagnerais à avoir un nouveau projet ? » after the jobs desk asked « … rattachée, ou un nouveau
projet ? »: the question stays with that desk (nora_chat_router._about_the_question), the desk's question rides with
the task (delivery_metadata « neoffice_ask_again »), and the notifier asks it again when the answer asks nothing.

Played on osiris on 09.11: the jobs pole answered for the customer and stopped, twice out of twice.
"""
import asyncio
import sys
import types
from types import SimpleNamespace

import pytest

from gateway import kanban_watchers_notifier as N
from gateway import nora_chat_router as R

CID = "nora-0123456789abcdef"
RID = f"{CID}:1793791466639"
_EXTRA = {
    "callback_url": "https://erp.example.test/api/method/nora.api.v2.hermes_callback.deliver",
    "callback_token": "tok",
    "conversation_id": CID,
}
ASKED = (
    "✋ Projets : Robert Martin a déjà un projet ouvert : « Maintenance informatique » (PROJ-2026-00258, intervention "
    "chez le client). L'intervention « régler le scanner » doit-elle y être rattachée, ou ouvrir un nouveau projet ?"
)
AGAIN = "L'intervention « régler le scanner » doit-elle y être rattachée, ou ouvrir un nouveau projet ?"


@pytest.fixture(autouse=True)
def fresh_state():
    dicts = (R._LAST_ROUTE, R._PENDING_SPACE, R._PENDING_NOTE, R._CONV_HISTORY, R._SPACE_ASKED, R._CANCELLED_REQUESTS)
    for d in dicts:
        d.clear()
    R._CURRENT_REQUEST.set("")
    yield
    for d in dicts:
        d.clear()
    R._CURRENT_REQUEST.set("")


# ── the sentence asked again ─────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "question, again",
    [
        (ASKED, AGAIN),
        (
            "✋ Projets : Lucien Favre n'a pas de projet ouvert. Quelle est la nature de l'intervention (ex. dépannage "
            "chaudière, maintenance…) ? Et a-t-elle une référence de sa part (n° de dossier) ?",
            "Et a-t-elle une référence de sa part (n° de dossier) ?",
        ),
        ("Projets — Voulez-vous la mettre dans le projet ouvert ?", "Voulez-vous la mettre dans le projet ouvert ?"),
        # the film keeps 400 characters: a question cut before its « ? » is not asked again
        ("✋ Projets : Robert Martin a déjà un projet ouvert : « Maintenance informatique » (PROJ-2026-00258, interv", ""),
        ("", ""),
    ],
)
def test_the_sentence_that_asks_is_the_one_asked_again(question, again):
    assert R._question_to_ask_again(question) == again


# ── the task carries it ──────────────────────────────────────────────────────────────────────────────────────────────


class _Resp:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return b"{}"


def _fake_kanban(monkeypatch):
    made = {"created": [], "subs": []}

    def create_task(conn, **kw):
        made["created"].append(kw)
        return "t_new"

    kanban_db = types.SimpleNamespace(create_task=create_task, archive_task=lambda conn, tid: True)
    connect = types.SimpleNamespace(connect=lambda board=None: SimpleNamespace(
        close=lambda: None, execute=lambda *a: SimpleNamespace(fetchall=list)))
    notify = types.SimpleNamespace(add_notify_sub=lambda conn, **kw: made["subs"].append(kw))
    pkg = sys.modules.get("hermes_cli") or types.ModuleType("hermes_cli")
    monkeypatch.setitem(sys.modules, "hermes_cli", pkg)
    for name, mod in (("kanban_db", kanban_db), ("kanban_db_connect", connect), ("kanban_db_notify", notify)):
        monkeypatch.setattr(pkg, name, mod, raising=False)
        monkeypatch.setitem(sys.modules, f"hermes_cli.{name}", mod)
    return made


def _route(monkeypatch, message):
    import urllib.request

    made = _fake_kanban(monkeypatch)
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _Resp())
    monkeypatch.setattr(R, "_asks_fast_answer", lambda *a, **k: False)
    # the jobs desk asked, and waits: the gateway's film holds what it asked
    monkeypatch.setattr(R, "_question_waiting", lambda conversation_id, board=None: "projet")
    R._CONV_HISTORY[CID] = ["User: Je dois aller chez Robert Martin pour régler son scanner.", "NORA: " + ASKED]
    R._LAST_ROUTE[CID] = {"msg": "Je dois aller chez Robert Martin pour régler son scanner.", "pole": "projet"}
    # the real decision: the fast path first, as classify does
    monkeypatch.setattr(R, "classify", lambda message, **kw: R._fast_path(message, kw.get("prior")) or "ventes")
    R.route_chat_message(
        message=message, session_chat_id="webhook:nora_chat:test", conversation_id=CID,
        thread_id=None, user_id="u", notifier_profile="default", idempotency_key=RID,
        call_llm_fn=lambda **kw: None, main_runtime=None, deliver_extra=dict(_EXTRA),
        chat_user="staff@example.test", language="fr", page_context=None, nora_spoke=True)
    return made


def test_a_question_about_the_question_carries_the_question_to_ask_again(monkeypatch):
    made = _route(monkeypatch, "Qu'est-ce que je gagnerais à avoir un nouveau projet ?")
    assert made["created"][0]["assignee"] == "projet"
    assert made["subs"][0]["delivery_metadata"]["neoffice_ask_again"] == AGAIN


def test_the_answer_itself_carries_nothing_to_ask_again(monkeypatch):
    made = _route(monkeypatch, "Mets-la dans le projet existant, jeudi à 14h.")
    assert made["created"][0]["assignee"] == "projet"
    assert "neoffice_ask_again" not in made["subs"][0]["delivery_metadata"]


# ── the notifier asks it again ───────────────────────────────────────────────────────────────────────────────────────


class _Adapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, msg, metadata=None):
        self.sent.append(msg)
        return SimpleNamespace(success=True)


async def _no_artifacts(**kw):
    return None


def _send(answer, metadata, kind="completed", payload=None):
    notification = N._KanbanNotification.__new__(N._KanbanNotification)
    adapter = _Adapter()
    notification.sub = {"chat_id": "webhook:nora_chat:test", "delivery_metadata": metadata}
    notification.adapter = adapter
    notification.platform_str = "webhook"
    notification.task_id = "t_new"
    notification.board_slug = "default"
    notification.task = None
    notification.runner = SimpleNamespace(_deliver_kanban_artifacts=_no_artifacts)
    ev = SimpleNamespace(kind=kind, payload=payload or {})
    assert asyncio.run(notification._send_event(ev, answer)) is True
    return adapter.sent[0]


ANSWER = (
    "Projets —\nPour Robert Martin, un nouveau projet vous donnerait sa propre facture, ses propres heures et sa propre "
    "marge."
)


def test_an_answer_that_asks_nothing_ends_with_the_question_still_waiting():
    sent = _send(ANSWER, {"conversation_id": CID, "neoffice_ask_again": AGAIN})
    assert sent == ANSWER + "\n\n" + AGAIN


def test_an_answer_that_asks_something_itself_is_left_alone():
    answer = ANSWER + " Voulez-vous l'ouvrir ?"
    assert _send(answer, {"conversation_id": CID, "neoffice_ask_again": AGAIN}) == answer


def test_a_task_that_names_no_question_is_delivered_as_it_is():
    assert _send(ANSWER, {"conversation_id": CID}) == ANSWER


def test_only_the_final_answer_asks_again():
    assert _send(ANSWER, {"conversation_id": CID, "neoffice_ask_again": AGAIN}, kind="blocked", payload={"kind": "needs_input"}) == ANSWER


def test_the_film_keeps_the_question_asked_again():
    _send(ANSWER, {"conversation_id": CID, "neoffice_ask_again": AGAIN})
    assert R._CONV_HISTORY[CID][-1].endswith(AGAIN)
