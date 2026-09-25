# //// Neoffice — added file (no upstream equivalent): a guardrail stop answers with what was found.
"""2026-09-25: a compta worker had four answers of five in hand when the loop guardrail stopped
it on a repeated empty listing; the person read « je n'ai pas réussi ». A kanban worker now asks
the model once more for what it found, and reads the answer where a worker puts it: the summary
of a kanban_complete, the reason of a kanban_block, or plain text."""
import json
import logging
from types import SimpleNamespace

import pytest

from agent import chat_completion_helpers as cch
from agent import neoffice_kanban_breaker as breaker
from agent import relay_llm
from agent.transports.types import ToolCall

DECISION = SimpleNamespace(tool_name="mcp__neoffice_compta__list_documents", code="identical_call_streak_halt")


@pytest.fixture
def summary_call(monkeypatch):
    """Replace the provider call: `answers` is what successive attempts return."""
    state = {"answers": [], "calls": 0}

    def build(agent, api_messages, request_id, seen):
        def attempt(retry_count):
            state["calls"] += 1
            answer = state["answers"].pop(0) if state["answers"] else ""
            if isinstance(answer, Exception):
                raise answer
            if not answer:
                seen.append("tool calls ['mcp__neoffice_compta__list_documents']")
            return answer
        return attempt

    monkeypatch.setattr(cch, "_iteration_summary_api_messages", lambda agent, messages: list(messages))
    monkeypatch.setattr(breaker, "_neoffice_summary_attempt", build)
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
    summary_call["answers"] = ["Soldes bancaires : 12 400 CHF. Échéances à 14 jours : aucune."]
    messages = [{"role": "user", "content": "revue de trésorerie"}]
    text = breaker.neoffice_guardrail_summary(_agent(), messages, DECISION)
    assert text == "Soldes bancaires : 12 400 CHF. Échéances à 14 jours : aucune."
    request = messages[-1]
    assert request["role"] == "user" and "list_documents" in request["content"]
    assert "kanban_complete" in request["content"] and "kanban_block" in request["content"]


def test_an_empty_first_attempt_is_asked_again(monkeypatch, summary_call):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test")
    summary_call["answers"] = ["", "Il me manque le code de l'article."]
    assert breaker.neoffice_guardrail_summary(_agent(), [{"role": "user", "content": "q"}], DECISION) == (
        "Il me manque le code de l'article.")
    assert summary_call["calls"] == 2


@pytest.mark.parametrize("answers", ([], ["", ""], [RuntimeError("provider down")]))
def test_no_summary_leaves_the_canned_message(monkeypatch, summary_call, answers):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test")
    summary_call["answers"] = list(answers)
    assert breaker.neoffice_guardrail_summary(_agent(), [{"role": "user", "content": "q"}], DECISION) == ""


def test_an_empty_summary_says_what_came_back(monkeypatch, summary_call, caplog):
    """10:13 on 2026-09-25 left no trace at all: the next one must name what the model returned."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test")
    summary_call["answers"] = ["", ""]
    with caplog.at_level(logging.WARNING, logger=breaker.logger.name):
        breaker.neoffice_guardrail_summary(_agent(), [{"role": "user", "content": "q"}], DECISION)
    assert "came back empty" in caplog.text and "list_documents']" in caplog.text


# ── the real attempt: where the answer is read ─────────────────────────────────────────────

def _call(name, **args):
    return ToolCall(id="c1", name=name, arguments=json.dumps(args, ensure_ascii=False))


@pytest.fixture
def one_response(monkeypatch):
    """Drive _neoffice_summary_attempt with one normalized response; `sent` is the request."""
    state = {"sent": {}, "response": None}

    def attempt_for(response):
        state["response"] = response
        agent = SimpleNamespace(
            api_mode="chat_completions",
            _build_api_kwargs=lambda msgs: {"model": "nora", "messages": msgs, "tools": [{"type": "function"}],
                                            "tool_choice": "auto"},
            _ensure_primary_openai_client=lambda reason: SimpleNamespace(),
            _get_transport=lambda: SimpleNamespace(normalize_response=lambda raw: state["response"]),
        )
        seen = []
        attempt = breaker._neoffice_summary_attempt(agent, [{"role": "user", "content": "q"}], "rid", seen)
        return attempt, seen

    monkeypatch.setattr(cch, "sanitize_outbound_kwargs", lambda agent, kwargs: None)
    monkeypatch.setattr(cch, "_managed_summary_call",
                        lambda agent, request_id, request, callback, retry_count: state["sent"].update(request) or "raw")
    monkeypatch.setattr(cch, "is_router_timeout_shim", lambda raw: False)
    state["attempt_for"] = attempt_for
    return state


def _normalized(content=None, tool_calls=None, finish="stop"):
    return SimpleNamespace(content=content, tool_calls=tool_calls, finish_reason=finish)


def test_the_tools_stay_declared(one_response):
    """Without them the model wrote its kanban_block as text and vLLM returned content=null (3/3)."""
    attempt, _ = one_response["attempt_for"](_normalized(content="Voici ce que j'ai trouvé."))
    assert attempt(0) == "Voici ce que j'ai trouvé."
    assert one_response["sent"]["tools"] and one_response["sent"]["tool_choice"] == "auto"


def test_the_reason_of_a_kanban_block_is_the_answer(one_response):
    """The ventes stop of 2026-09-25 10:13, replayed: 3/3 answered with kanban_block."""
    question = "Je n'ai pas trouvé d'article « appui ». Pouvez-vous me donner son code exact ?"
    attempt, _ = one_response["attempt_for"](_normalized(
        tool_calls=[_call("kanban_block", kind="needs_input", reason=question)], finish="tool_calls"))
    assert attempt(0) == question


@pytest.mark.parametrize("args, expected", (
    ({"summary": "Facture FA-1 : 2 lignes ajoutées.", "result": "détail"}, "Facture FA-1 : 2 lignes ajoutées."),
    ({"result": "Solde : 12 400 CHF."}, "Solde : 12 400 CHF."),
))
def test_the_summary_of_a_kanban_complete_is_the_answer(one_response, args, expected):
    attempt, _ = one_response["attempt_for"](_normalized(tool_calls=[_call("kanban_complete", **args)]))
    assert attempt(0) == expected


def test_a_terminal_call_inside_the_tool_search_bridge(one_response):
    """Written as text, the call came wrapped: tool_call with calls=[{name, arguments}]."""
    bridge = _call("tool_call", calls=[{"name": "kanban_block", "arguments": {"reason": "Quel article ?"}}])
    attempt, _ = one_response["attempt_for"](_normalized(tool_calls=[bridge]))
    assert attempt(0) == "Quel article ?"


def test_a_lookup_call_is_no_answer_and_is_named(one_response):
    attempt, seen = one_response["attempt_for"](_normalized(
        tool_calls=[_call("mcp__neoffice_ventes__frappe_search_item", query="appui")], finish="tool_calls"))
    assert attempt(0) == ""
    assert seen == ["tool calls ['mcp__neoffice_ventes__frappe_search_item']"]


def test_plain_text_without_its_think_block(one_response):
    attempt, _ = one_response["attempt_for"](_normalized(content="<think>x</think>\nIl me manque le prix."))
    assert attempt(0) == "Il me manque le prix."


def test_a_think_only_answer_is_empty(one_response):
    attempt, seen = one_response["attempt_for"](_normalized(content="<think>only</think>"))
    assert attempt(0) == "" and seen == ["no text (finish=stop)"]


# ── a fork turn (the post-task skill review) is not the worker ──────────────────────────────
# 2026-09-25: the first skill review that ever started (#717) inherited HERMES_KANBAN_TASK, and
# the worker's no-progress breaker cut it after three refused skill_manage calls.

def test_the_breaker_leaves_a_review_alone(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test")
    review = SimpleNamespace(_turn_origin="background_review")
    verdict = breaker.neoffice_kanban_no_progress_breaker(
        review, assistant_message=SimpleNamespace(tool_calls=None, content="…"), messages=[],
        final_response="", _turn_exit_reason=None)
    assert verdict.action == "fallthrough"
    assert not hasattr(review, "_kanban_no_progress_streak"), "the worker's streak is not the review's"


def test_a_review_stopped_by_the_guardrail_asks_no_summary(monkeypatch, summary_call):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test")
    summary_call["answers"] = ["should not be asked"]
    review = SimpleNamespace(api_mode="chat_completions", _turn_origin="background_review")
    messages = [{"role": "user", "content": "q"}]
    assert breaker.neoffice_guardrail_summary(review, messages, DECISION) == ""
    assert summary_call["calls"] == 0 and len(messages) == 1
