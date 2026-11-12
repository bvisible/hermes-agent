# //// Neoffice — added file: the test of upstream PR #137817 (open on 2026-11-12), ported with what our port
# //// adds: the note to the model, and the MCP tools a pin keeps loaded (tools.tool_search.eager).
"""A ``tool_call`` naming a tool the session lists directly is run, not refused (#137639).

The bridge exists to reach the tools a session does NOT list. A name it does list carries an unambiguous
intent: the dispatcher runs the call the model meant and says so in the result, instead of a refusal that
costs a whole turn (2 to 15 a day per pole worker on our development instance, pinned MCP tools mostly).
The session's grant does not widen: a name outside it, a deferred name and a multi-entry local batch keep
their own answers.
"""

from __future__ import annotations

import json

import pytest

import tools.tool_search as tool_search
from model_tools import _DIRECT_CALL, _dispatch_bridge_tool, handle_function_call
from tools.tool_search import DIRECT_CALL_NOTE, directly_listed_target


def _bridge_call(name: str, arguments: dict) -> dict:
    return {"calls": [{"name": name, "arguments": arguments}]}


def _td(name: str) -> dict:
    return {"type": "function", "function": {"name": name, "description": name,
                                             "parameters": {"type": "object", "properties": {}}}}


PINNED = "mcp__tcd_sales__party_360"
DEFERRED = "mcp__tcd_sales__sales_deposit"


@pytest.fixture
def pinned_sales(monkeypatch):
    """Two MCP tools of one pole server, the first pinned eager: the shape of a pole worker's scope."""
    from tools.registry import registry

    for name in (PINNED, DEFERRED):
        registry.register(name=name, handler=lambda args, **kw: json.dumps({"ok": True}),
                          schema=_td(name)["function"], toolset="mcp-tcd-sales")
    monkeypatch.setattr(tool_search, "_eager_tool_names", lambda: frozenset({PINNED}))
    return [_td(PINNED), _td(DEFERRED)]


def test_bridge_unwraps_a_directly_listed_tool_for_local_dispatch():
    result, underlying = _dispatch_bridge_tool(
        "tool_call", _bridge_call("read_file", {"path": "/tmp/direct-listing-probe.txt"}), ["file"], None)
    assert result is None
    assert underlying == ("read_file", {"path": "/tmp/direct-listing-probe.txt"}, _DIRECT_CALL)


def test_handle_function_call_runs_the_directly_listed_tool_and_says_so(tmp_path):
    target = tmp_path / "direct.txt"
    target.write_text("BRIDGE-DIRECT-DISPATCH", encoding="utf-8")
    out = handle_function_call("tool_call", _bridge_call("read_file", {"path": str(target)}),
                               enabled_toolsets=["file"])
    assert "BRIDGE-DIRECT-DISPATCH" in out
    assert "directly-listed tool, not a deferred one" not in out
    assert out.endswith(DIRECT_CALL_NOTE.format(name="read_file"))


def test_direct_dispatch_does_not_widen_the_session_grant():
    # ``terminal`` is a real registered tool, but this session grants only ``file``: still refused.
    result, underlying = _dispatch_bridge_tool(
        "tool_call", _bridge_call("terminal", {"command": "echo nope"}), ["file"], None)
    assert underlying is None
    assert "terminal" in json.loads(result)["error"]


def test_a_pinned_mcp_tool_is_a_direct_call(pinned_sales):
    assert directly_listed_target(_bridge_call(PINNED, {"name": "Fleuriste"}), pinned_sales) == (
        PINNED, {"name": "Fleuriste"})


def test_a_deferred_mcp_tool_keeps_the_deferred_path(pinned_sales):
    assert directly_listed_target(_bridge_call(DEFERRED, {}), pinned_sales) is None


def test_a_local_batch_keeps_its_own_answer(pinned_sales):
    batch = {"calls": [{"name": PINNED, "arguments": {}}, {"name": "read_file", "arguments": {"path": "/x"}}]}
    assert directly_listed_target(batch, pinned_sales + [_td("read_file")]) is None
