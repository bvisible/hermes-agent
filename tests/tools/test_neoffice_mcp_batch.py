# //// Neoffice — added file (no upstream equivalent): a batch of MCP tools runs entry by
# //// entry instead of costing the model a turn (#710), and the compta guard still sees a
# //// chart read made inside such a batch (#422).
"""Two pole tools asked in one tool_call run, in order, each through the single-call path.

Measured on the dev instance over three days (2026-09-24): 226 of 405 compta sessions had
a batch refused with "Local tools require one entry per tool_call" — typically the chart
of accounts and the doctrine search that the imputation prompt asks for as steps 1 and 2
— and repeated each tool alone: one lost model turn per task. MCP tools hold no
agent-bound state, so they may share a tool_call; any other local tool keeps upstream's
refusal (test_connector_local_batches pins it).
"""

import json

import pytest

CHART = "mcp__neoffice_compta__get_chart_of_accounts"
WIKI = "mcp__neoffice_wiki__wiki_search"


def test_a_batch_of_mcp_tools_resolves_to_one_dispatch_unit():
    from tools.tool_search import CONNECTOR_BATCH_SENTINEL, resolve_underlying_call

    calls = [{"name": CHART, "arguments": {"search": "pneus"}}, {"name": WIKI, "arguments": {"query": "pneus"}}]
    name, args, err = resolve_underlying_call({"calls": calls})
    assert err is None and name == CONNECTOR_BATCH_SENTINEL
    assert [c["name"] for c in args["calls"]] == [CHART, WIKI]


def test_an_agent_bound_local_tool_still_cannot_share_a_call():
    from tools.tool_search import resolve_underlying_call

    name, _args, err = resolve_underlying_call({"calls": [
        {"name": CHART, "arguments": {}}, {"name": "todo_list", "arguments": {}}]})
    assert name is None and "one entry per tool_call" in err


def test_each_mcp_entry_goes_through_the_single_call_path_in_order(monkeypatch):
    import model_tools
    from tools.connectors.dispatch import dispatch_connector_batch

    seen = []

    def single(name, args, **_kw):
        seen.append((name, args))
        inner = args["calls"][0]["name"]
        if inner == WIKI:
            return json.dumps({"error": "wiki unavailable"})
        return json.dumps({"accounts": ["4200 - Achats"]})

    monkeypatch.setattr(model_tools, "handle_function_call", single)
    out = json.loads(dispatch_connector_batch(
        [{"name": CHART, "arguments": {"search": "pneus"}}, {"name": WIKI, "arguments": {"query": "pneus"}}],
        model_tools._CallIds(), user_task=None, enabled_tools=None, middleware_trace=[],
        enabled_toolsets=None, disabled_toolsets=None))

    # Each entry re-entered tool_call on its own: the session scope and the schema probe
    # of _dispatch_bridge_tool apply to it exactly as when it is called alone.
    assert [name for name, _ in seen] == ["tool_call", "tool_call"]
    assert [a["calls"][0]["name"] for _, a in seen] == [CHART, WIKI]
    assert out["success_count"] == 1 and out["error_count"] == 1
    assert out["results"][0]["name"] == CHART and "response" in out["results"][0]
    assert out["results"][1]["name"] == WIKI and "error" in out["results"][1]


@pytest.mark.parametrize("entry, counts", [
    ({"index": 0, "name": CHART, "response": {"accounts": ["6400 - Electricité"]}}, True),
    ({"index": 0, "name": CHART, "error": {"code": "TOOL_ERROR", "message": "timeout"}}, False),
    ({"index": 0, "name": WIKI, "response": "doctrine"}, False),
])
def test_the_compta_guard_reads_a_chart_read_inside_a_batch(monkeypatch, tmp_path, entry, counts):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    from hermes_state import SessionDB
    from tools import kanban_tools as kt

    sid = "sess-batched-chart"
    batch = {"results": [entry], "success_count": 1, "error_count": 0, "total_count": 1}
    with SessionDB() as db:
        db.create_session(sid, "cli")
        db.append_message(sid, "tool", tool_name="tool_call",
                          content='<untrusted_tool_result source="tool_call">' + json.dumps(batch)
                                  + "</untrusted_tool_result>")
    assert kt._neoffice_session_ran_tool(sid, "get_chart_of_accounts") is counts
