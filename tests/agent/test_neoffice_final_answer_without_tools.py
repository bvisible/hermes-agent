# //// Neoffice — added file (no upstream equivalent): a worker's last answer asked again without tools (#3016).
"""On the accounting exam of 18.11, 13 workers stopped by the loop guardrail answered the summary request with another
lookup (frappe_calc, frappe_entry_check, wiki_search): the call was neither run nor read, the summary came back empty
twice and the card ended done on the canned stop text. With the profile key, the second attempt sends tool_choice
"none" and reads a call the model writes as text; a card still unanswered is blocked, never closed done."""
import json
from types import SimpleNamespace

import pytest

from agent import chat_completion_helpers as cch
from agent import neoffice_kanban_breaker as breaker
from agent import relay_llm
from agent.transports.types import ToolCall

DECISION = SimpleNamespace(tool_name="mcp__neoffice_compta__wiki_search", code="idempotent_no_progress_block")
ANSWER = "TVA due : 7,7 % sur 1 000 CHF, soit 77 CHF. Il me manque la date de la facture."
WRITTEN_COMPLETE = (f"<tool_call>\n<function=kanban_complete>\n<parameter=summary>\n{ANSWER}\n</parameter>\n"
                    "</function>\n</tool_call>")
WRITTEN_LOOKUP = ("<tool_call>\n<function=mcp__neoffice_compta__frappe_calc>\n<parameter=expression>\n1000*0.077\n"
                  "</parameter>\n</function>\n</tool_call>")


def _guard(on):
    return SimpleNamespace(config=SimpleNamespace(neoffice_final_answer_without_tools=on))


def _call(name, **args):
    return ToolCall(id="c1", name=name, arguments=json.dumps(args, ensure_ascii=False))


def _normalized(content=None, tool_calls=None, finish="stop"):
    return SimpleNamespace(content=content, tool_calls=tool_calls, finish_reason=finish)


@pytest.fixture
def provider(monkeypatch):
    """The real attempts against a fake provider: `responses` answer in turn, `sent` keeps every request."""
    state = {"responses": [], "sent": []}

    def agent(on):
        return SimpleNamespace(
            api_mode="chat_completions", provider="custom", client=None, _tool_guardrails=_guard(on),
            _build_api_kwargs=lambda msgs: {"model": "nora", "messages": msgs, "tools": [{"type": "function"}],
                                            "tool_choice": "auto"},
            _ensure_primary_openai_client=lambda reason: SimpleNamespace(),
            _interruptible_api_call=lambda kwargs: None,
            _get_transport=lambda: SimpleNamespace(normalize_response=lambda raw, **kw: state["responses"].pop(0)),
        )

    def managed(agent, request_id, request, callback, retry_count):
        state["sent"].append(dict(request))
        return "raw"

    monkeypatch.setattr(cch, "sanitize_outbound_kwargs", lambda agent, kwargs: None)
    monkeypatch.setattr(cch, "_managed_summary_call", managed)
    monkeypatch.setattr(cch, "is_router_timeout_shim", lambda raw: False)
    monkeypatch.setattr(cch, "_iteration_summary_api_messages", lambda agent, messages: list(messages))
    monkeypatch.setattr(relay_llm, "complete_logical_call", lambda *a, **kw: None)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test")
    state["agent"] = agent
    return state


def _summary(state, on):
    return breaker.neoffice_guardrail_summary(state["agent"](on), [{"role": "user", "content": "q"}], DECISION)


# ── the guardrail stop ───────────────────────────────────────────────────────────────────────

def test_a_lookup_then_no_tools_gives_the_answer(provider):
    provider["responses"] = [_normalized(tool_calls=[_call("mcp__neoffice_compta__frappe_calc", expression="1")]),
                             _normalized(content=ANSWER)]
    assert _summary(provider, on=True) == ANSWER
    first, second = provider["sent"]
    assert first["tool_choice"] == "auto"
    assert second["tool_choice"] == "none" and second["tools"], "the tools stay declared: the cached prefix holds"


def test_without_the_key_the_second_attempt_is_the_first_again(provider):
    provider["responses"] = [_normalized(tool_calls=[_call("mcp__neoffice_compta__frappe_calc", expression="1")]),
                             _normalized(content=ANSWER)]
    assert _summary(provider, on=False) == ANSWER
    assert [request["tool_choice"] for request in provider["sent"]] == ["auto", "auto"]


def test_a_kanban_complete_written_as_text_is_the_answer(provider):
    provider["responses"] = [_normalized(tool_calls=[_call("mcp__neoffice_compta__wiki_search", query="TVA")]),
                             _normalized(content=WRITTEN_COMPLETE)]
    assert _summary(provider, on=True) == ANSWER


def test_a_lookup_written_as_text_is_no_answer(provider):
    provider["responses"] = [_normalized(tool_calls=[_call("mcp__neoffice_compta__wiki_search", query="TVA")]),
                             _normalized(content=WRITTEN_LOOKUP)]
    assert _summary(provider, on=True) == ""


@pytest.mark.parametrize("words", [
    pytest.param("Je vérifie le calcul.", id="an-announcement"),
    pytest.param(ANSWER, id="a-real-answer"),
])
def test_text_then_a_written_lookup_is_no_answer_and_the_text_goes_with_the_block(provider, words):
    # A written call says the model meant to go on: closing the card on the sentence before it would read as done.
    provider["responses"] = [_normalized(tool_calls=[_call("mcp__neoffice_compta__wiki_search", query="TVA")]),
                             _normalized(content=f"{words}\n{WRITTEN_LOOKUP}")]
    agent = provider["agent"](True)
    assert breaker.neoffice_guardrail_summary(agent, [{"role": "user", "content": "q"}], DECISION) == ""
    assert breaker.neoffice_unanswered_text(agent) == words


def test_an_answer_without_a_call_closes(provider):
    provider["responses"] = [_normalized(tool_calls=[_call("mcp__neoffice_compta__wiki_search", query="TVA")]),
                             _normalized(content=ANSWER)]
    agent = provider["agent"](True)
    assert breaker.neoffice_guardrail_summary(agent, [{"role": "user", "content": "q"}], DECISION) == ANSWER
    assert breaker.neoffice_unanswered_text(agent) == ""


@pytest.mark.parametrize("content, expected", [
    (f"Je vérifie.\n{WRITTEN_LOOKUP}", "Je vérifie."),
    (f"```\n{WRITTEN_LOOKUP}\n```", ""),
    (f"Voici :\n```xml\n{WRITTEN_LOOKUP}\n```", "Voici :"),
    ("Je regarde.\n<function=mcp__neoffice_compta__wiki_search>\n<parameter=query>\nTVA", "Je regarde."),
    ("<think>calcul</think>\n" + WRITTEN_LOOKUP, ""),
    (f"Calcul :\n```\n1000*0.077\n```\n\n```\n77\n```\n{WRITTEN_LOOKUP}",
     "Calcul :\n```\n1000*0.077\n```\n\n```\n77\n```"),
    (f"Exemple : ```print(1)```\n{WRITTEN_LOOKUP}", "Exemple : ```print(1)```"),
])
def test_the_text_around_written_lookups(content, expected):
    assert breaker.neoffice_written_lookup_text(content) == expected


@pytest.mark.parametrize("content, expected", [
    (WRITTEN_COMPLETE, ANSWER),
    (WRITTEN_LOOKUP, ""),
    (f"{ANSWER}\n{WRITTEN_LOOKUP}", ""),
    (f"{WRITTEN_LOOKUP}\n{ANSWER}", ""),
    ("<function=mcp__neoffice_compta__wiki_search>\n<parameter=query>\nTVA\n</parameter>", ""),
    ('<tool_call>{"name": "kanban_block", "arguments": {"reason": "Quelle date ?"}}</tool_call>', "Quelle date ?"),
    ("<function=kanban_block>\n<parameter=reason>\nQuelle date ?\n</parameter>", "Quelle date ?"),
    ("Réponse sans appel.", None),
    ("", None),
    (None, None),
])
def test_a_written_call(content, expected):
    assert breaker.neoffice_written_call_text(content) == expected


# ── a card still unanswered is blocked, never closed done ────────────────────────────────────

@pytest.fixture
def blocks(monkeypatch):
    from tools import kanban_tools

    seen = []
    monkeypatch.setattr(kanban_tools, "_handle_block", lambda args, **kw: seen.append(args) or json.dumps({"ok": True}))
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test")
    return seen


def test_an_unanswered_card_is_blocked_on_the_stop_text(blocks):
    agent = SimpleNamespace(_tool_guardrails=_guard(True))
    assert breaker.neoffice_block_unanswered(agent, "I stopped retrying because I kept running the same search.")
    assert blocks == [{"reason": "I stopped retrying because I kept running the same search."}]


def test_without_the_key_the_card_is_left_as_before(blocks):
    assert not breaker.neoffice_block_unanswered(SimpleNamespace(_tool_guardrails=_guard(False)), "stop text")
    assert not breaker.neoffice_block_unanswered(SimpleNamespace(), "stop text")
    assert blocks == []


def test_a_fork_turn_or_no_worker_blocks_nothing(blocks, monkeypatch):
    review = SimpleNamespace(_tool_guardrails=_guard(True), _turn_origin="background_review")
    assert not breaker.neoffice_block_unanswered(review, "stop text")
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    assert not breaker.neoffice_block_unanswered(SimpleNamespace(_tool_guardrails=_guard(True)), "stop text")
    assert blocks == []


def test_a_refused_block_says_so_and_leaves_the_card(monkeypatch, caplog):
    from tools import kanban_tools

    monkeypatch.setattr(kanban_tools, "_handle_block", lambda args, **kw: json.dumps({"error": "goal_mode task"}))
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test")
    assert not breaker.neoffice_block_unanswered(SimpleNamespace(_tool_guardrails=_guard(True)), "stop text")
    assert "refused" in caplog.text


# ── the iteration limit: upstream's summary, its retry asked without tools ───────────────────

def test_the_iteration_limit_retry_is_asked_without_tools(provider):
    agent = provider["agent"](True)
    provider["responses"] = [_normalized(tool_calls=[_call("mcp__neoffice_compta__frappe_calc", expression="1")]),
                             _normalized(content=WRITTEN_COMPLETE)]
    attempt = cch._chat_summary_attempt(agent, [{"role": "user", "content": "q"}], "rid")
    assert attempt(0) == "" and attempt(1) == ANSWER
    assert [request["tool_choice"] for request in provider["sent"]] == ["auto", "none"]


def test_at_the_iteration_limit_the_text_before_a_written_lookup_is_the_summary(provider):
    agent = provider["agent"](True)
    provider["responses"] = [_normalized(content=""), _normalized(content=f"{ANSWER}\n{WRITTEN_LOOKUP}")]
    attempt = cch._chat_summary_attempt(agent, [{"role": "user", "content": "q"}], "rid")
    attempt(0)
    assert attempt(1) == ANSWER, "with the key off upstream sends this answer: it is not lost, only its markup is"


def test_at_the_iteration_limit_a_bare_written_lookup_is_no_answer(provider):
    agent = provider["agent"](True)
    provider["responses"] = [_normalized(content=""), _normalized(content=WRITTEN_LOOKUP)]
    attempt = cch._chat_summary_attempt(agent, [{"role": "user", "content": "q"}], "rid")
    attempt(0)
    assert attempt(1) == "", "no text at all: upstream's retry and stop text follow"


def test_without_the_key_the_iteration_limit_keeps_upstreams_request(provider):
    agent = provider["agent"](False)
    provider["responses"] = [_normalized(content=""), _normalized(content=WRITTEN_COMPLETE)]
    attempt = cch._chat_summary_attempt(agent, [{"role": "user", "content": "q"}], "rid")
    attempt(0)
    assert attempt(1) == WRITTEN_COMPLETE.strip(), "upstream's plain read: the written call is not interpreted"
    assert [request["tool_choice"] for request in provider["sent"]] == ["auto", "auto"]


def test_the_key_is_read_from_the_profile():
    from agent.tool_guardrails import ToolCallGuardrailConfig

    assert ToolCallGuardrailConfig.from_mapping({}).neoffice_final_answer_without_tools is False
    assert ToolCallGuardrailConfig.from_mapping(
        {"neoffice_final_answer_without_tools": True}).neoffice_final_answer_without_tools is True


# ── the tool round hands an unanswered stop to the block (agent/turn_tool_round.py) ──────────

def _halted_run(monkeypatch, summary, words=""):
    """A real agent loop stopped by the guardrail; the summary and the block are stubbed to see the branch.

    `words` is what the stubbed summary records as written around lookups (neoffice_unanswered_text)."""
    from unittest.mock import patch

    from tests.agent.test_tool_call_guardrail_runtime import (
        _hard_stop_config,
        _make_agent,
        _mock_response,
        _mock_tool_call,
    )

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    agent = _make_agent("web_search", max_iterations=10, config=_hard_stop_config())
    agent.client.chat.completions.create.side_effect = [
        _mock_response(content="", finish_reason="tool_calls",
                       tool_calls=[_mock_tool_call("web_search", json.dumps({"query": "same"}), f"c{i}")])
        for i in range(1, 10)
    ]
    agent._disable_streaming = True
    handed = []
    def stub_summary(agent, messages, decision):
        agent._neoffice_unanswered_text = words
        return summary

    monkeypatch.setattr(breaker, "neoffice_guardrail_summary", stub_summary)
    monkeypatch.setattr(breaker, "neoffice_block_unanswered", lambda agent, text: handed.append(text) or True)
    with (
        patch("model_tools.handle_function_call", return_value=json.dumps({"error": "boom"})),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("search repeatedly")
    return result, handed


def test_the_tool_round_hands_an_unanswered_stop_to_the_block(monkeypatch):
    result, handed = _halted_run(monkeypatch, "")
    assert result["turn_exit_reason"] == "guardrail_halt"
    assert result["final_response"] and handed == [result["final_response"]]


def test_the_tool_round_never_blocks_an_answered_stop(monkeypatch):
    result, handed = _halted_run(monkeypatch, ANSWER)
    assert result["turn_exit_reason"] == "guardrail_halt"
    assert result["final_response"] == ANSWER and handed == []


def test_the_tool_round_blocks_with_the_models_own_words(monkeypatch):
    result, handed = _halted_run(monkeypatch, "", words="Je vérifie le calcul.")
    assert result["turn_exit_reason"] == "guardrail_halt"
    assert handed == ["Je vérifie le calcul."] and result["final_response"] == "Je vérifie le calcul."

