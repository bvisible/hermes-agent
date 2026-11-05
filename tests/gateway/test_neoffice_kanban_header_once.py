# //// Neoffice — added file (no upstream equivalent): a delivered answer carries its header once.
"""A worker that copied the delivery header at the top of its handoff is not headed twice (osiris, 10.10)."""
from types import SimpleNamespace

from gateway.kanban_watchers_notifier import _neoffice_fmt_completed

TITLE = "Et pour suivre ce que nous devons à nos fournisseurs ?"
OLD_HEAD = f"✅ Comptabilité — {TITLE}"
HEAD = "Comptabilité —"
ANSWER = "Le rapport « Créanciers » est fait pour ça."


def _deliver(full):
    task = SimpleNamespace(assignee="compta", body=TITLE, result=None)
    message, _wake, _extra = _neoffice_fmt_completed(SimpleNamespace(payload={}), SimpleNamespace(
        d={"full_summary": full}, task=task, title=TITLE))
    return message


def test_a_header_the_worker_repeated_is_delivered_once():
    assert _deliver(f"{OLD_HEAD}\n{ANSWER}") == f"{HEAD}\n{ANSWER}"
    assert _deliver(f"{OLD_HEAD} {ANSWER}") == f"{HEAD}\n{ANSWER}"
    assert _deliver(f"{HEAD}\n{ANSWER}") == f"{HEAD}\n{ANSWER}"
    assert _deliver(f"{HEAD} {ANSWER}") == f"{HEAD}\n{ANSWER}"


def test_the_head_names_the_desk_and_neither_a_check_mark_nor_the_persons_message():
    # 05.11: « ✅ » sat on turns that changed nothing, and the title is what the person had just written.
    first = _deliver(ANSWER).split("\n")[0]
    assert "✅" not in first and TITLE not in first


def test_the_voice_console_can_still_drop_the_head_and_read_the_desk():
    # nora_console _spokenAnswer drops a first line holding « — », _answerPole reads « <label> — » there.
    first, rest = _deliver(ANSWER).split("\n", 1)
    assert "—" in first and first.startswith("Comptabilité") and rest == ANSWER


def test_a_worker_that_left_no_summary_still_says_what_was_done():
    assert _deliver("") == OLD_HEAD


def test_an_answer_without_it_is_headed_as_before():
    assert _deliver(ANSWER) == f"{HEAD}\n{ANSWER}"


def test_a_line_that_only_mentions_the_pole_is_kept():
    text = "✅ Comptabilité a vérifié les montants.\nTout est en ordre."
    assert _deliver(text) == f"{HEAD}\n{text}"
