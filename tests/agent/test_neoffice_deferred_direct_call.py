# //// Neoffice — added file (no upstream equivalent): a deferred tool called by its own name (10.10).
"""A worker found « send_email » with tool_search, read it with tool_describe, then called it by its own
name. Upstream's fuzzy repair read « mcp__neoffice_ventes__send_email » as « mcp__neoffice_ventes__search_link »
(the shared server prefix makes them close), and the e-mail's arguments went to the link search nine times.
The call now goes through the tool_call bridge, and the repair never swaps one tool of a server for another.
"""
import json
from types import SimpleNamespace

from run_agent import AIAgent

SEND = "mcp__neoffice_ventes__send_email"
SEARCH = "mcp__neoffice_ventes__search_link"
GET = "mcp__neoffice_ventes__get_document"
BRIDGE = {"tool_search", "tool_describe", "tool_call"}
MAIL = {"attach_document": {"doctype": "Purchase Order", "name": "PO-1"}, "message": "Bonjour"}


def _agent(valid):
    # Real AIAgent methods (id uniquifying, name repair), no init side effects.
    agent = AIAgent.__new__(AIAgent)
    agent.provider, agent.model, agent.session_id, agent.tools = "nous", "m", "s1", []
    agent.valid_tool_names = set(valid)
    agent.log_prefix, agent._base_url = "", "http://localhost"
    agent._invalid_tool_retries = agent._invalid_json_retries = 0
    agent._print_fn = lambda *args, **kwargs: None  # the repair and error notices print through it
    return agent


def _tc(name, args):
    return SimpleNamespace(id="call_1", type="function",
                           function=SimpleNamespace(name=name, arguments=json.dumps(args)))


def _validate(agent, calls, monkeypatch, scoped):
    import agent.tool_executor as executor
    from agent.turn_tool_validation import validate_tool_calls

    monkeypatch.setattr(executor, "_tool_search_scoped_names", lambda _agent: frozenset(scoped))
    return validate_tool_calls(agent, SimpleNamespace(content="", tool_calls=calls), "tool_calls",
                               messages=[], conversation_history=[], api_call_count=1, effective_task_id="t1")


def test_a_deferred_tool_called_by_its_name_goes_through_the_bridge(monkeypatch):
    call = _tc(SEND, MAIL)
    verdict = _validate(_agent(BRIDGE | {SEARCH, GET}), [call], monkeypatch, {SEND})
    assert verdict.action == "ok"
    assert call.function.name == "tool_call"
    assert json.loads(call.function.arguments) == {"calls": [{"name": SEND, "arguments": MAIL}]}


def test_a_bare_name_of_one_deferred_tool_is_called_by_its_full_name(monkeypatch):
    call = _tc("send_email", MAIL)
    verdict = _validate(_agent(BRIDGE | {SEARCH, GET}), [call], monkeypatch, {SEND})
    assert verdict.action == "ok"
    assert json.loads(call.function.arguments)["calls"][0]["name"] == SEND


def test_a_tool_out_of_the_session_is_neither_routed_nor_swapped(monkeypatch):
    call = _tc(SEND, MAIL)
    verdict = _validate(_agent(BRIDGE | {SEARCH, GET}), [call], monkeypatch, set())
    assert call.function.name == SEND  # left unknown: the model is told, it is not sent elsewhere
    assert verdict.action != "ok"


def test_without_the_bridge_nothing_is_routed(monkeypatch):
    call = _tc(SEND, MAIL)
    _validate(_agent({SEARCH, GET}), [call], monkeypatch, {SEND})
    assert call.function.name == SEND


def test_unreadable_arguments_keep_upstreams_path(monkeypatch):
    call = SimpleNamespace(id="call_1", type="function", function=SimpleNamespace(name=SEND, arguments="{not json"))
    _validate(_agent(BRIDGE | {SEARCH, GET}), [call], monkeypatch, {SEND})
    assert call.function.name == SEND


def test_a_spelling_repair_within_one_tool_still_works(monkeypatch):
    call = _tc("mcp__neoffice_ventes__Get_Document", {"doctype": "Purchase Order", "name": "PO-1"})
    verdict = _validate(_agent(BRIDGE | {SEARCH, GET}), [call], monkeypatch, set())
    assert verdict.action == "ok" and call.function.name == GET


def test_one_tool_of_a_server_is_never_another():
    from agent.turn_tool_validation import _other_mcp_tool

    assert _other_mcp_tool(SEND, SEARCH)
    assert not _other_mcp_tool("mcp__neoffice_ventes__Send_Email", SEND)
    assert not _other_mcp_tool(SEND, "mcp__neoffice_compta__send_email")  # another server: not this guard's case
    assert not _other_mcp_tool("send_email", SEND)
