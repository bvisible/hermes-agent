# //// Neoffice — added file (no upstream equivalent): tool_search names the tools a pin keeps
# //// loaded (tools.tool_search.eager), apart from the deferred matches.
"""A pinned tool sits in the model's tool list and not in the deferred catalog, so tool_search
could never name it. Capability bench, 2026-09-26: a sales worker searched « frappe party
contact update » five times, got only a generic field setter, and concluded the tool did not
exist, while that exact tool was loaded. The search now reports loaded MCP tools under
``loaded``, with one note saying to call them directly; the rest of the answer is unchanged."""

import json

import pytest

import tools.tool_search as tool_search
from tools.tool_search import ToolSearchConfig, dispatch_tool_search


def _td(name, desc, props=None, required=None):
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props or {}, "required": required or []}}}


def _register(name, toolset, desc, props=None, required=None):
    from tools.registry import registry

    registry.register(name=name, handler=lambda args, **kw: json.dumps({"ok": True}),
                      schema=_td(name, desc, props, required), toolset=toolset)
    return _td(name, desc, props, required)


PINNED = "mcp__tsl_sales__party_contact_update"
PINNED_DOC = "mcp__tsl_sales__create_document"
SETTER = "mcp__tsl_sales__update_field"
QUOTE = "mcp__tsl_sales__quotation_create"


@pytest.fixture
def sales_defs(monkeypatch):
    """A sales worker's scope: two pinned tools, two deferred ones, one core tool."""
    defs = [
        _register(PINNED, "mcp-tsl-sales",
                  "Update a customer's contact: e-mail address and phone of its primary contact.",
                  {"party": {"type": "string"}, "email_id": {"type": "string"}}, ["party"]),
        _register(PINNED_DOC, "mcp-tsl-sales",
                  "Create a new draft document of any doctype (Lead, Task, Address).",
                  {"doctype": {"type": "string"}, "data": {"type": "object"}}, ["doctype", "data"]),
        _register(SETTER, "mcp-tsl-sales",
                  "Update one field on any document: a party name, a contact e-mail, a price.",
                  {"doctype": {"type": "string"}, "fieldname": {"type": "string"}}),
        _register(QUOTE, "mcp-tsl-sales",
                  "Create a quotation for a customer, two steps: preview, then confirm.",
                  {"customer": {"type": "string"}}, ["customer"]),
        _td("terminal", "Run a shell command and update files."),
    ]
    monkeypatch.setattr(tool_search, "_eager_tool_names", lambda: frozenset({PINNED, PINNED_DOC}))
    return defs


def _search(queries, defs, **extra):
    return json.loads(dispatch_tool_search({"queries": queries, **extra}, current_tool_defs=defs,
                                           config=ToolSearchConfig.from_raw({})))


def test_a_pinned_tool_the_query_names_comes_back_under_loaded(sales_defs):
    result = _search(["party contact update"], sales_defs)
    group = result["results"][0]
    assert group["loaded"] == [PINNED]
    assert result["loaded_note"] == tool_search._LOADED_NOTE
    assert "call them directly" in result["loaded_note"] and "tool_call" in result["loaded_note"]
    # The deferred answer is what it always was: the setter, and a record for it only.
    assert group["matches"] == [SETTER]
    assert set(result["tools"]) == {SETTER}


def test_the_exact_name_of_a_pinned_tool_is_found(sales_defs):
    group = _search([PINNED], sales_defs)["results"][0]
    assert group["loaded"][0] == PINNED


def test_deferred_matches_are_unchanged_by_pinned_tools(sales_defs):
    """When every query has a deferred match, everything but the two new keys is exactly the
    answer given without the pinned tools (a group with no deferred match loses its retry hint
    to the note instead: see test_a_loaded_hit_replaces_the_retry_hint)."""
    queries = ["party contact update", "create quotation", "update field document"]
    with_pinned = _search(queries, sales_defs)
    without = _search(queries, [td for td in sales_defs
                                if td["function"]["name"] not in {PINNED, PINNED_DOC}])
    with_pinned.pop("loaded_note", None)
    for group in with_pinned["results"]:
        group.pop("loaded", None)
    assert with_pinned == without


def test_a_query_that_names_no_pinned_tool_lists_none(sales_defs):
    result = _search(["create quotation"], sales_defs)
    assert result["results"][0]["matches"] == [QUOTE]
    assert "loaded" not in result["results"][0]
    assert "loaded_note" not in result


def test_core_tools_are_never_reported_as_loaded(sales_defs):
    result = _search(["run shell command"], sales_defs)
    assert "loaded" not in result["results"][0] and "loaded_note" not in result


def test_a_loaded_hit_replaces_the_retry_hint(sales_defs):
    """No deferred match but a loaded one: no « retry before concluding » hint, the note says
    where the capability is."""
    result = _search(["primary contact phone"], sales_defs)
    group = result["results"][0]
    assert group["matches"] == [] and group["loaded"] == [PINNED]
    assert "hint" not in group and "available_sources" not in group
    assert "loaded_note" in result


def test_the_retry_hint_stays_when_nothing_matches(sales_defs):
    group = _search(["payroll salary slip"], sales_defs)["results"][0]
    assert group["matches"] == [] and "loaded" not in group
    assert "hint" in group and "available_sources" in group


def test_limit_applies_to_loaded_hits(sales_defs):
    """« address » names both pinned tools and no deferred one."""
    assert len(_search(["address"], sales_defs)["results"][0]["loaded"]) == 2
    assert len(_search(["address"], sales_defs, limit=1)["results"][0]["loaded"]) == 1


def test_a_pinned_tool_outranked_by_deferred_ones_is_not_offered(monkeypatch):
    """Only what the search would have offered before the pin: the top ``limit`` of one search
    over deferred + loaded tools. With two slots, « create quotation » belongs to the two
    quotation tools, and the generic create tool is not pushed as loaded; with three it is."""
    defs = [_register("mcp__tsl_quote__quotation_create", "mcp-tsl-quote", "Create a quotation."),
            _register("mcp__tsl_quote__quotation_from_order", "mcp-tsl-quote",
                      "Create a quotation from a sales order."),
            _register("mcp__tsl_quote__create_any", "mcp-tsl-quote", "Create any draft document."),
            _register("mcp__tsl_quote__draft_line", "mcp-tsl-quote", "Add a line to a draft quotation.")]
    monkeypatch.setattr(tool_search, "_eager_tool_names", lambda: frozenset(
        {"mcp__tsl_quote__create_any", "mcp__tsl_quote__draft_line"}))
    two = _search(["create quotation"], defs, limit=2)["results"][0]
    assert two["matches"] == ["mcp__tsl_quote__quotation_create", "mcp__tsl_quote__quotation_from_order"]
    assert "loaded" not in two
    assert _search(["create quotation"], defs, limit=3)["results"][0]["loaded"] == ["mcp__tsl_quote__create_any"]


def test_describe_and_call_still_send_a_pinned_tool_to_the_direct_path(sales_defs):
    """Unchanged: a pinned tool is not reachable through the bridge; the answer says to call
    it directly, which is what the search note now says up front."""
    described = json.loads(tool_search.dispatch_tool_describe({"names": [PINNED]},
                                                              current_tool_defs=sales_defs))
    assert PINNED in described["errors"] and PINNED not in described["tools"]
    name, _args, err = tool_search.resolve_underlying_call(
        {"calls": [{"name": PINNED, "arguments": {"party": "C-1"}}]})
    assert name is None and err
    name, _args, err = tool_search.resolve_underlying_call(
        {"calls": [{"name": QUOTE, "arguments": {"customer": "C-1"}}]})
    assert name == QUOTE and err is None
