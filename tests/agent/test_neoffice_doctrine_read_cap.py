# //// Neoffice — added file (no upstream equivalent): a per-turn ceiling on reads of the profile's doctrine tools
# //// (neoffice-maintenance#3125)
"""A per-turn ceiling on reads of the profile's doctrine tools (#3125).

Measured on one pole's log over nine days: 6 of 2,263 turns read the doctrine more than 20 times (21 to 24 reads), with
queries that changed at each call, which the no-progress detector (identical signature and result) does not see. The
cause of those long turns is not established; the ceiling bounds them. loop_caps.neoffice_max_doctrine_reads counts the
calls to the tools the profile names in neoffice_doctrine_tools, direct or through the tool_search bridge, and blocks
the one past the ceiling; the stopped worker is then asked for its answer with a request that states what happened.
Off (0, the default) or without doctrine tools: nothing is counted and no decision changes.

How the stop reaches the card (the real loop, the real guardrail, the board) is played in
test_neoffice_guardrail_card_question.py, next to the stop it shares its floor with.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent.tool_guardrails import ToolCallGuardrailConfig, ToolCallGuardrailController

SEARCH = "mcp__neoffice_wiki__wiki_search"
READ = "mcp__neoffice_wiki__wiki_read"
RESULT = json.dumps({"results": ["a page"]})


def _controller(cap=3, tools=("wiki_search", "wiki_read")):
    return ToolCallGuardrailController(ToolCallGuardrailConfig.from_mapping({
        "neoffice_doctrine_tools": list(tools), "loop_caps": {"neoffice_max_doctrine_reads": cap}}))


def test_the_read_past_the_ceiling_is_blocked_whatever_the_query():
    guard = _controller(cap=3)
    for n, tool in enumerate((SEARCH, READ, SEARCH)):
        assert guard.before_call(tool, {"query": f"q{n}"}).action == "allow", n
    decision = guard.before_call(SEARCH, {"query": "q3"})
    assert (decision.action, decision.code, decision.should_halt) == ("block", "neoffice_doctrine_read_cap", True)
    assert (decision.count, decision.tool_name) == (3, SEARCH)
    assert guard.halt_decision == decision


def test_the_stop_message_is_the_template_filled_with_what_the_code_knows():
    from agent.tool_guardrails import _DECISION_MESSAGES, NEOFFICE_DOCTRINE_READ_CAP_CODE

    guard = _controller(cap=3)
    for n in range(3):
        guard.before_call(SEARCH, {"query": f"q{n}"})
    decision = guard.before_call(READ, {"page": "p"})
    template = _DECISION_MESSAGES[NEOFFICE_DOCTRINE_READ_CAP_CODE]
    assert decision.code == NEOFFICE_DOCTRINE_READ_CAP_CODE == "neoffice_doctrine_read_cap"
    assert decision.message == template.format(tool_name=READ, count=3, cap=3)
    for claim in ("does not answer", "does not cover", "silent"):
        assert claim not in template, "the code knows how many searches were made, not what they found"


def test_reads_through_the_tool_search_bridge_count_each():
    guard = _controller(cap=3)
    bridge = {"calls": [{"name": READ, "arguments": {"page": "a"}}, {"name": "wiki_search", "arguments": {}},
                        {"name": "mcp__neoffice_compta__frappe_calc", "arguments": {}}]}
    assert guard.before_call("tool_call", bridge).action == "allow"
    assert guard.before_call(SEARCH, {"query": "x"}).action == "allow"
    assert guard.before_call(SEARCH, {"query": "y"}).code == "neoffice_doctrine_read_cap"


def test_a_bridge_batch_is_counted_per_entry_after_the_loop_splits_it():
    """The loop splits a batch before the guard sees it (agent/tool_call_batches.py): the guard receives one entry per
    call, the shape the real loop delivers."""
    from agent.tool_call_batches import expand_local_tool_batches

    entries = [{"name": READ, "arguments": {"page": "a"}}, {"name": SEARCH, "arguments": {"query": "b"}},
               {"name": "mcp__neoffice_compta__frappe_calc", "arguments": {"expression": "1+1"}}]
    batch = SimpleNamespace(id="b1", type="function", function=SimpleNamespace(
        name="tool_call", arguments=json.dumps({"calls": entries})))
    expanded = expand_local_tool_batches([batch])
    assert len(expanded) == 3
    guard = _controller(cap=2)
    for call in expanded:
        args = json.loads(call.function.arguments)
        assert len(args["calls"]) == 1, "one entry per call"
        assert guard.before_call(call.function.name, args).action == "allow"
    assert guard.before_call(SEARCH, {"query": "c"}).code == "neoffice_doctrine_read_cap", "two reads were counted"


def test_other_tools_are_not_counted():
    guard = _controller(cap=2)
    for n in range(10):
        assert guard.before_call("mcp__neoffice_compta__frappe_calc", {"expression": str(n)}).action == "allow"
    assert guard.before_call(SEARCH, {"query": "a"}).action == "allow"


@pytest.mark.parametrize("cap, tools", [(0, ("wiki_search", "wiki_read")), (3, ())])
def test_off_or_without_doctrine_tools_nothing_is_counted(cap, tools):
    guard = _controller(cap=cap, tools=tools)
    for n in range(40):
        assert guard.before_call(SEARCH, {"query": f"q{n}"}).action == "allow", n


def test_the_ceiling_is_absent_by_default_and_read_from_the_profile():
    assert ToolCallGuardrailConfig.from_mapping({}).loop_caps.neoffice_max_doctrine_reads == 0
    assert _controller(cap=12).config.loop_caps.neoffice_max_doctrine_reads == 12


@pytest.mark.parametrize("value, expected", [
    ("20", 20), (20, 20), (2.7, 2),                       # whole numbers, as every upstream cap reads them
    (None, 0), ("abc", 0), ("20.0", 0), ([], 0), (-3, 0),  # anything else is off, never an error
])
def test_the_ceiling_value_is_a_whole_number_or_the_ceiling_is_off(value, expected):
    config = ToolCallGuardrailConfig.from_mapping({
        "neoffice_doctrine_tools": ["wiki_read"], "loop_caps": {"neoffice_max_doctrine_reads": value}})
    assert config.loop_caps.neoffice_max_doctrine_reads == expected


def test_a_new_turn_starts_again_from_zero():
    guard = _controller(cap=1)
    assert guard.before_call(SEARCH, {"query": "a"}).action == "allow"
    assert guard.before_call(SEARCH, {"query": "b"}).action == "block"
    guard.reset_for_turn()
    assert guard.before_call(SEARCH, {"query": "c"}).action == "allow"


def _decisions(mapping):
    """Every decision, whole, of one fixed run with the hard stop on: three different reads, one read repeated until
    upstream's own no-progress detector blocks it, a bridge call, a tool that is no read."""
    guard = ToolCallGuardrailController(ToolCallGuardrailConfig.from_mapping({
        "hard_stop_enabled": True, "neoffice_doctrine_tools": ["wiki_search", "wiki_read"], **mapping}))
    run = [(SEARCH, {"query": "q1"}), (READ, {"page": "a"}), (SEARCH, {"query": "q2"})]
    run += [(READ, {"page": "same"})] * 7
    run += [("tool_call", {"calls": [{"name": "wiki_search", "arguments": {"query": "q3"}}]}),
            ("mcp__neoffice_compta__frappe_calc", {"expression": "1+1"})]
    seen = []
    for tool, args in run:
        before = guard.before_call(tool, args)
        seen.append(before)
        if before.allows_execution:
            seen.append(guard.after_call(tool, args, RESULT, failed=False))
    seen.append(guard.halt_decision)
    return seen


def test_with_the_ceiling_off_or_not_reached_every_decision_is_the_same_as_without_it():
    """Keys off, nothing changes, by the values: the whole decision (action, code, message, count, signature) of each
    call and the turn's halt decision, with the hard stop on."""
    without = _decisions({})
    assert any(d is not None and d.code == "idempotent_no_progress_block" for d in without), \
        "the run reaches upstream's own stop, so the comparison is not empty"
    assert _decisions({"loop_caps": {"neoffice_max_doctrine_reads": 0}}) == without
    assert _decisions({"loop_caps": {"neoffice_max_doctrine_reads": 50}}) == without, "on but never reached"


def test_the_stopped_worker_is_asked_with_the_ceilings_own_request():
    from agent.neoffice_kanban_breaker import (
        NEOFFICE_DOCTRINE_CAP_SUMMARY_REQUEST,
        NEOFFICE_GUARDRAIL_SUMMARY_REQUEST,
        neoffice_summary_request,
    )

    capped = neoffice_summary_request(SimpleNamespace(code="neoffice_doctrine_read_cap", count=12, tool_name=SEARCH))
    assert capped == NEOFFICE_DOCTRINE_CAP_SUMMARY_REQUEST.format(count=12)
    stopped = neoffice_summary_request(SimpleNamespace(code="idempotent_no_progress_block", count=5, tool_name=SEARCH))
    assert stopped == NEOFFICE_GUARDRAIL_SUMMARY_REQUEST.format(tool=SEARCH), "the other stops keep their request"
    nameless = neoffice_summary_request(SimpleNamespace(code="loop_web_search_cap", count=3, tool_name=""))
    assert nameless == NEOFFICE_GUARDRAIL_SUMMARY_REQUEST.format(tool="a tool")
    for claim in ("does not answer", "does not cover", "same result"):
        assert claim not in NEOFFICE_DOCTRINE_CAP_SUMMARY_REQUEST, "it states the count of searches, nothing else"
