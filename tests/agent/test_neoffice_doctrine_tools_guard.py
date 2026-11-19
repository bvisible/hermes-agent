# //// Neoffice — added file (no upstream equivalent): doctrine reads served by MCP count as read-only (#3016).
"""A NORA pole worker ran the same doctrine search again and again, interleaved with other calls: on the accounting
and HR exams 6 to 18 questions per pass repeated one search more than twice without chaining it. Upstream's read-only
list is frozen and holds no MCP tool, so the no-progress detector (the one that counts non-consecutive repeats) never
saw them. A profile now names its doctrine tools; unnamed, nothing changes."""
import pytest

from agent.tool_guardrails import (
    ToolCallGuardrailConfig,
    ToolCallGuardrailController,
    is_neoffice_doctrine_tool,
)

SEARCH = "mcp__neoffice_wiki__wiki_search"
OTHER = "mcp__neoffice_compta__get_chart_of_accounts"
DOCTRINE = ["wiki_search", "wiki_read", "frappe_knowledge_page"]
RESULT = '{"result": "[{\\"path\\": \\"wiki/concepts/tva.md\\"}]"}'


def _controller(**config):
    data = {"hard_stop_enabled": True, **config}
    return ToolCallGuardrailController(ToolCallGuardrailConfig.from_mapping(data, platform="cli"))


def _search(guard, query="TVA forfaitaire", result=RESULT):
    """One doctrine search followed by one other call: the repeats never chain."""
    before = guard.before_call(SEARCH, {"query": query, "limit": 5})
    if not before.allows_execution:
        return before, None
    after = guard.after_call(SEARCH, {"query": query, "limit": 5}, result, failed=False)
    guard.before_call(OTHER, {})
    guard.after_call(OTHER, {}, '{"result": "plan"}', failed=False)
    return before, after


def test_unnamed_nothing_changes():
    guard = _controller()
    for _ in range(6):
        before, after = _search(guard)
        assert before.action == "allow" and after.action == "allow"
    assert guard.halt_decision is None


def test_a_named_doctrine_search_is_warned_at_its_second_identical_result():
    guard = _controller(neoffice_doctrine_tools=DOCTRINE)
    _, first = _search(guard)
    _, second = _search(guard)
    assert first.action == "allow"
    assert second.action == "warn" and second.code == "idempotent_no_progress_warning"
    assert "Use the result already provided" in second.message


def test_a_named_doctrine_search_is_blocked_once_it_returned_the_same_result_five_times():
    guard = _controller(neoffice_doctrine_tools=DOCTRINE)
    for _ in range(5):
        _search(guard)
    before, _ = _search(guard)
    assert before.action == "block" and before.code == "idempotent_no_progress_block"
    assert guard.halt_decision is not None


def test_the_profile_threshold_blocks_the_third_identical_search():
    # the exam criterion: no identical doctrine search more than twice per question
    guard = _controller(neoffice_doctrine_tools=DOCTRINE, hard_stop_after={"idempotent_no_progress": 2})
    _search(guard)
    _search(guard)
    before, _ = _search(guard)
    assert before.action == "block"


def test_another_query_or_another_result_is_progress():
    guard = _controller(neoffice_doctrine_tools=DOCTRINE)
    _search(guard, query="TVA forfaitaire")
    _, other_query = _search(guard, query="TVA dette fiscale nette")
    assert other_query.action == "allow"
    _, other_result = _search(guard, query="TVA forfaitaire", result='{"result": "[]"}')
    assert other_result.action == "allow"


@pytest.mark.parametrize("name, named", [
    ("mcp__neoffice_wiki__wiki_search", True),
    ("mcp__neoffice_compta__wiki_search", True),
    ("wiki_read", True),
    ("mcp__neoffice_ventes__frappe_knowledge_page", True),
    ("mcp__neoffice_wiki__wiki_search_more", False),
    ("mcp__neoffice_compta__wiki_searcher", False),
    ("mcp__neoffice_compta__create_document", False),
])
def test_a_name_matches_the_tool_or_the_last_segment_of_an_mcp_tool(name, named):
    assert is_neoffice_doctrine_tool(name, frozenset(DOCTRINE)) is named


def test_a_mutating_tool_is_never_read_only_even_named():
    guard = _controller(neoffice_doctrine_tools=["memory"])
    assert not guard._is_idempotent("memory")


@pytest.mark.parametrize("value", [None, "wiki_search", 3, {"wiki_search": True}])
def test_a_malformed_setting_names_nothing(value):
    assert ToolCallGuardrailConfig.from_mapping({"neoffice_doctrine_tools": value}).neoffice_doctrine_tools == frozenset()
