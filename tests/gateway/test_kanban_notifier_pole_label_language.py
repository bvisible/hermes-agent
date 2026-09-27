# //// Neoffice — added file (no upstream equivalent): the pole's name in a delivered answer
# //// follows the language the worker was told to answer in (#867).
"""An English answer is headed « Accounting », a German one « Buchhaltung »."""
from types import SimpleNamespace

import pytest

from gateway.kanban_watchers_notifier import _neoffice_head


def _n(assignee, body):
    return SimpleNamespace(task=SimpleNamespace(assignee=assignee, body=body))


@pytest.mark.parametrize("language, label", [
    ("English", "Accounting"),
    ("German", "Buchhaltung"),
    ("Italian", "Contabilità"),
    ("French", "Comptabilité"),
])
def test_the_label_speaks_the_answers_language(language, label):
    body = f"How many overdue invoices?\n\n[Reply to the user in {language}. Do not reply in any other language.]"
    assert _neoffice_head(_n("compta", body)) == label


def test_a_task_without_the_directive_stays_french():
    assert _neoffice_head(_n("ventes", "Liste des devis")) == "Ventes"
    assert _neoffice_head(SimpleNamespace(task=None)) in ("Le service",)
