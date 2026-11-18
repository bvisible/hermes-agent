# //// Neoffice — added file (no upstream equivalent): a cut-off card turn asks its person instead of crashing (#2755)
"""A card whose turn was cut off at the output limit is blocked `needs_input` (neoffice-maintenance#2755).

Such a turn ends incomplete with failure_reason "truncated" and returns early, before finalize_turn. It is not
in-place retry authority, so the worker used to exit non-zero with its card still running: a crash counted against
the card, a run from scratch into the same cut, and after two the card was abandoned with nothing said. While the
worker still owns its live run, the card is now blocked `needs_input` through the worker's own kanban_block
handler, with the branding catalog's sentence as its question. Never `done`, nothing counted, no comment.

The turn results are built by the real truncation code (turn_truncation._Trunc.end_turn), with the arguments
each of its endings passes.
"""

import json
import os
from pathlib import Path

import pytest

from agent import turn_truncation as tt
from agent.kanban_turn_recovery import max_recovery_attempts, recover_failed_kanban_turns
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from neoffice_branding import reply, strip_internal_mechanics

PARTIAL = "Line 400: input tax on goods. Line 405: input tax on investments and on operating costs, which also"
CEILING = "Response remained truncated after 4 continuation attempts"


class _Agent:
    """What end_turn touches on the agent (resource cleanup, session persistence): no-ops."""

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def _end_turn(final_response, error=None, **kwargs) -> dict:
    """One ending of a cut-off turn, as turn_truncation builds it."""
    st = tt._Trunc(
        action="fallthrough", result=None, messages=[{"role": "user", "content": "the question"}],
        length_continue_retries=4, truncated_response_parts=[], truncated_tool_call_retries=0, retry_count=0,
        compression_attempts=0, agent=_Agent(), response=None, finish_reason="length", conversation_history=None,
        api_call_count=5, effective_task_id=None, current_turn_user_idx=0,
    )
    return st.end_turn(final_response, error, **kwargs).result


ENDINGS = {
    # Continuations spent, some text received (turn_truncation._continue_text).
    "ceiling with text": lambda: _end_turn(PARTIAL, CEILING),
    # Continuations spent, nothing visible received.
    "ceiling without text": lambda: _end_turn(tt._CEILING_NO_TEXT, CEILING),
    # The prompt filled the context window.
    "window full": lambda: _end_turn(
        f"{PARTIAL}\n\n{tt._WINDOW_FILLED.format(prompt=31000, ctx=32768)}",
        "Prompt used 31000 of 32768 context tokens; no room to answer",
    ),
    # A tool call cut mid-arguments again (turn_truncation._retry_truncated_tool_call).
    "tool call cut": lambda: _end_turn(tt._TRUNCATED_FINAL, cleanup=False, failure=("truncated", True)),
    # The first response of the turn was cut: failed.
    "first response cut": lambda: _end_turn(tt._FIRST_TRUNCATED_FINAL, cleanup=False, failed=True),
}


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB, in-place recovery at its default; the profile speaks French."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_DB", "HERMES_KANBAN_CLAIM_LOCK",
                "HERMES_KANBAN_TURN_RECOVERY"):
        monkeypatch.delenv(var, raising=False)
    import agent.i18n

    monkeypatch.setattr(agent.i18n, "get_language", lambda: "fr")
    kb.init_db()
    return home


def _running_card(monkeypatch):
    """A card claimed by this worker, pinned as the dispatcher pins it: (task id, run id)."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="a long accounting question", assignee="compta")
        host = kb._claimer_id().split(":", 1)[0]
        kb.claim_task(conn, tid, claimer=f"{host}:w")
        task = kb.get_task(conn, tid)
        conn.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (os.getpid(), tid))
        conn.execute("UPDATE task_runs SET worker_pid=? WHERE id=?", (os.getpid(), task.current_run_id))
        conn.commit()
        task = kb.get_task(conn, tid)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(task.current_run_id))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(kb.kanban_db_path()))
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", task.claim_lock or "")
    return tid, task.current_run_id


def _settle(result, *, tid=None, live=None) -> int:
    """What a worker does after its turn, returning the in-place attempts made (the turn stays cut each time);
    ``live`` stands for the gateway's bound-claim proof."""
    kwargs = {"task_id": tid} if tid else {}
    if live is not None:
        kwargs["claim_check"] = lambda: live
    return recover_failed_kanban_turns(lambda _nudge: None, lambda: result, sleep_fn=lambda _s: None, **kwargs)


def _state(tid):
    with kbc.connect() as conn:
        task = kb.get_task(conn, tid)
        blocked = [
            json.loads(r[0] or "{}")
            for r in conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='blocked'", (tid,))
        ]
        comments = kb.list_comments(conn, tid)
    return task, blocked, comments


@pytest.mark.parametrize("ending", sorted(ENDINGS))
def test_a_cut_off_card_asks_its_person_and_counts_nothing(kanban_home, monkeypatch, ending):
    result = ENDINGS[ending]()
    assert result["failure_reason"] == "truncated"  # the real ending, not a hand-made shape
    tid, _run = _running_card(monkeypatch)
    attempts = _settle(result)  # the CLI worker: task id and claim proof from the dispatcher's pins
    # Only a failed ending is upstream's in-place retry authority; it is rerun first, then the card asks.
    assert attempts == (max_recovery_attempts() if result.get("failed") else 0)
    task, blocked, comments = _state(tid)
    assert (task.status, task.block_kind, task.consecutive_failures) == ("blocked", "needs_input", 0)
    assert [b.get("reason") for b in blocked] == [reply("truncated_card", "fr")]
    assert comments == []


def test_the_gateway_worker_blocks_on_its_bound_claim(kanban_home, monkeypatch):
    tid, _run = _running_card(monkeypatch)
    _settle(ENDINGS["ceiling with text"](), tid=tid, live=True)
    assert _state(tid)[0].status == "blocked"


@pytest.mark.parametrize("lang", ["fr", "de", "it", "en"])
def test_the_question_is_in_the_profiles_language(kanban_home, monkeypatch, lang):
    """Written through the worker's block handler (its redaction, block_task, the event's JSON), word for word."""
    import agent.i18n

    monkeypatch.setattr(agent.i18n, "get_language", lambda: lang)
    tid, _run = _running_card(monkeypatch)
    _settle(ENDINGS["ceiling with text"]())
    assert [b.get("reason") for b in _state(tid)[1]] == [reply("truncated_card", lang)]


@pytest.mark.parametrize("lang", ["fr", "de", "it", "en"])
def test_the_question_reaches_the_person_as_written(lang):
    """The webhook delivery filter replaces upstream's failure copy whole; this sentence passes untouched."""
    question = reply("truncated_card", lang)
    assert question and strip_internal_mechanics(question, lang) == question
    assert "continue" not in question.lower()


# A compta card about an account goes through one more filter on its way to the chat
# (gateway.kanban_watchers._safe_accounting_block_reason): only the questions of its block reason are kept. A
# sentence that asked nothing was replaced by the filter's fallback, « Quelle information factuelle manque-t-il
# pour départager les traitements comptables possibles ? », in French whatever the person's language: a false
# question about an answer that was only cut (seen with the deployed code on the work instance, 18.11).
_CHAT_CARDS = {
    "sales": ("ventes", "Devis pour une pose de parquet", "Prépare un devis."),
    "compta, no account": ("compta", "Relance des factures en retard", "Liste les factures en retard."),
    "compta, about an account": (
        "compta",
        "Sur quel compte imputer une facture de nettoyage ?",
        "Dans quel compte passer cette facture ?",
    ),
}


@pytest.mark.parametrize("card", list(_CHAT_CARDS))
@pytest.mark.parametrize("lang", ["fr", "de", "it", "en"])
def test_the_chat_shows_the_question_whatever_the_card(lang, card):
    """The whole delivery of the block on a customer chat: the notifier's formatter, then the webhook's filter."""
    import types

    from gateway import kanban_watchers_notifier as notifier

    assignee, title, body = _CHAT_CARDS[card]
    question = reply("truncated_card", lang)
    task = types.SimpleNamespace(assignee=assignee, title=title, body=body, status="blocked", result="")
    note = types.SimpleNamespace(task=task, title=title, platform_str="webhook")
    event = types.SimpleNamespace(kind="blocked", payload={"reason": question, "kind": "needs_input"})
    text, _wake, _extra = notifier._EVENT_FORMATTERS["blocked"](event, note)
    assert question in strip_internal_mechanics(text, lang)


def test_a_worker_that_lost_its_claim_changes_nothing(kanban_home, monkeypatch):
    tid, _run = _running_card(monkeypatch)
    _settle(ENDINGS["ceiling with text"](), tid=tid, live=False)
    task, blocked, _ = _state(tid)
    assert (task.status, blocked) == ("running", [])


def test_a_stale_run_is_refused_by_the_handlers_ownership_proof(kanban_home, monkeypatch):
    tid, run = _running_card(monkeypatch)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run + 1000))  # a worker of an older or other run
    _settle(ENDINGS["ceiling with text"](), tid=tid, live=True)
    task, blocked, _ = _state(tid)
    assert (task.status, blocked) == ("running", [])


@pytest.mark.parametrize("shape", [
    {"failed": True, "failure_reason": "loop_error", "failure_retryable": False},
    {"final_response": "done", "messages": [], "completed": True},
    {**ENDINGS["ceiling with text"](), "interrupted": True},
])
def test_any_other_turn_is_left_as_upstream_books_it(kanban_home, monkeypatch, shape):
    tid, _run = _running_card(monkeypatch)
    _settle(shape)
    assert _state(tid)[0].status == "running"
