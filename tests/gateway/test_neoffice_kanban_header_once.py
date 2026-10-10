# //// Neoffice — added file (no upstream equivalent): a delivered answer carries its header once.
"""A worker that copied the delivery header at the top of its handoff is not headed twice (osiris, 10.10)."""
from types import SimpleNamespace

from gateway.kanban_watchers_notifier import _neoffice_fmt_completed

TITLE = "Et pour suivre ce que nous devons à nos fournisseurs ?"
HEAD = f"✅ Comptabilité — {TITLE}"
ANSWER = "Le rapport « Créanciers » est fait pour ça."


def _deliver(full):
    task = SimpleNamespace(assignee="compta", body=TITLE, result=None)
    message, _wake, _extra = _neoffice_fmt_completed(SimpleNamespace(payload={}), SimpleNamespace(
        d={"full_summary": full}, task=task, title=TITLE))
    return message


def test_a_header_the_worker_repeated_is_delivered_once():
    assert _deliver(f"{HEAD}\n{ANSWER}") == f"{HEAD}\n{ANSWER}"
    assert _deliver(f"{HEAD} {ANSWER}") == f"{HEAD}\n{ANSWER}"


def test_an_answer_without_it_is_headed_as_before():
    assert _deliver(ANSWER) == f"{HEAD}\n{ANSWER}"


def test_a_line_that_only_mentions_the_pole_is_kept():
    text = "✅ Comptabilité a vérifié les montants.\nTout est en ordre."
    assert _deliver(text) == f"{HEAD}\n{text}"
