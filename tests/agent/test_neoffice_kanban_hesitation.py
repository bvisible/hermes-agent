"""The hesitation budget of a kanban worker.

# //// Neoffice — added file (no upstream equivalent).
#
# A worker whose pole has no tool for the request rebuilt the answer by hand for two minutes
# (« Qui paie en retard ? » in rh, 27.09). After a number of tool rounds it is told once to
# finish or to ask the user one short question, and four rounds later to finish now.
"""
from types import SimpleNamespace

import pytest

from agent.neoffice_kanban_breaker import (
    neoffice_hesitation_nudge,
    neoffice_kanban_no_progress_breaker,
)


def test_the_budget_speaks_twice_and_only_twice():
    said = {n: neoffice_hesitation_nudge(n, first=8) for n in range(1, 20)}
    assert [n for n, text in said.items() if text] == [8, 12]
    assert "kanban_block" in said[8] and "ONE short question" in said[8]
    assert "finish now" in said[12]


def test_zero_turns_it_off():
    assert not any(neoffice_hesitation_nudge(n, first=0) for n in range(1, 30))


def test_the_default_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("NEOFFICE_KANBAN_HESITATION_ROUNDS", "3")
    assert neoffice_hesitation_nudge(3) and not neoffice_hesitation_nudge(8)
    monkeypatch.setenv("NEOFFICE_KANBAN_HESITATION_ROUNDS", "junk")
    assert neoffice_hesitation_nudge(8)


def _business_round():
    return SimpleNamespace(tool_calls=[SimpleNamespace(function=SimpleNamespace(name="list_documents"))])


@pytest.fixture
def worker(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_hesitation")
    monkeypatch.delenv("NEOFFICE_KANBAN_HESITATION_ROUNDS", raising=False)
    return SimpleNamespace()


def test_a_worker_is_told_on_its_eighth_and_twelfth_rounds(worker):
    messages, actions = [], []
    for _ in range(13):
        verdict = neoffice_kanban_no_progress_breaker(
            worker, assistant_message=_business_round(), messages=messages,
            final_response=None, _turn_exit_reason=None,
        )
        actions.append(verdict.action)
    assert [i + 1 for i, a in enumerate(actions) if a == "continue"] == [8, 12]
    nudges = [m for m in messages if m.get("_kanban_hesitation_nudge")]
    assert len(nudges) == 2 and all(m["role"] == "user" for m in nudges)


def test_the_orchestrator_is_untouched(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    agent, messages = SimpleNamespace(), []
    for _ in range(12):
        verdict = neoffice_kanban_no_progress_breaker(
            agent, assistant_message=_business_round(), messages=messages,
            final_response=None, _turn_exit_reason=None,
        )
        assert verdict.action == "fallthrough"
    assert messages == []
