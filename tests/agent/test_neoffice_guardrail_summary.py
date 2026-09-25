# //// Neoffice — added file (no upstream equivalent): a guardrail stop answers with what was found.
"""2026-09-25: a compta worker had four answers of five in hand when the loop guardrail stopped
it on a repeated empty listing; the person read « je n'ai pas réussi ». A kanban worker now asks
the model once, without tools, for what it found and what it could not get."""
from types import SimpleNamespace

import pytest

from agent import chat_completion_helpers as cch
from agent import neoffice_kanban_breaker as breaker
from agent import relay_llm

DECISION = SimpleNamespace(tool_name="mcp__neoffice_compta__list_documents", code="identical_call_streak_halt")


@pytest.fixture
def summary_call(monkeypatch):
    """Replace the provider call: `answers` is what successive attempts return."""
    state = {"answers": [], "calls": 0}

    def build(agent, api_messages, request_id):
        def attempt(retry_count):
            state["calls"] += 1
            answer = state["answers"].pop(0) if state["answers"] else ""
            if isinstance(answer, Exception):
                raise answer
            return answer
        return attempt

    monkeypatch.setattr(cch, "_iteration_summary_api_messages", lambda agent, messages: list(messages))
    monkeypatch.setattr(cch, "_chat_summary_attempt", build)
    monkeypatch.setattr(relay_llm, "complete_logical_call", lambda *a, **kw: None)
    return state


def _agent():
    return SimpleNamespace(api_mode="chat_completions")


def test_outside_a_kanban_worker_nothing_changes(monkeypatch, summary_call):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    messages = [{"role": "user", "content": "q"}]
    assert breaker.neoffice_guardrail_summary(_agent(), messages, DECISION) == ""
    assert messages == [{"role": "user", "content": "q"}] and summary_call["calls"] == 0


def test_a_worker_answers_with_what_it_found(monkeypatch, summary_call):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test")
    summary_call["answers"] = ["<think>x</think>Soldes bancaires : 12 400 CHF. Échéances à 14 jours : aucune."]
    messages = [{"role": "user", "content": "revue de trésorerie"}]
    text = breaker.neoffice_guardrail_summary(_agent(), messages, DECISION)
    assert text == "Soldes bancaires : 12 400 CHF. Échéances à 14 jours : aucune."
    assert messages[-1]["role"] == "user" and "list_documents" in messages[-1]["content"]
    assert "Do not call any tool" in messages[-1]["content"]


@pytest.mark.parametrize("answers", ([], ["", ""], [RuntimeError("provider down")], ["<think>only</think>"]))
def test_no_summary_leaves_the_canned_message(monkeypatch, summary_call, answers):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test")
    summary_call["answers"] = list(answers)
    assert breaker.neoffice_guardrail_summary(_agent(), [{"role": "user", "content": "q"}], DECISION) == ""
