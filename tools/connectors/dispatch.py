"""Route connector calls through the normal dispatch policy pipeline."""

import json
from dataclasses import asdict

from tools.registry import tool_error
from tools.connectors.gateway.config import MAX_CALLS_PER_DISPATCH
from tools.connectors.gateway.merge import assemble_results, fill_remote_failure, partition_calls


def dispatch_connector_call(name, arguments, tool_call_id, turn_id=None, task_id=None):
    from tools.connectors.data_use import record_data_use
    from tools.connectors.gateway.bridge import run_remote

    partition = partition_calls([{"name": name, "arguments": arguments}])
    entries = run_remote(partition.remote, tool_call_id, availability=None, client_factory=None, turn_id=turn_id,
                         task_id=task_id)
    entry = entries[0]
    record_data_use(entry.get("data_use") or ())
    return json.dumps({key: value for key, value in entry.items() if key in {"response", "error", "account"}},
                      ensure_ascii=False)


def dispatch_connector_batch(calls, ids, *, user_task, enabled_tools,
                             middleware_trace, enabled_toolsets, disabled_toolsets):
    from dataclasses import replace

    from model_tools import handle_function_call, suppress_post_tool_call_hook
    from tools.interrupt import is_interrupted

    if len(calls) > MAX_CALLS_PER_DISPATCH:
        return tool_error(f"too many calls: {len(calls)} > max {MAX_CALLS_PER_DISPATCH}. "
                          "Retry with fewer calls per batch.")
    partition = partition_calls(calls)
    # //// Neoffice — MCP entries run here too, each through the single-call path (#710):
    # //// wrapping one entry in its own tool_call re-enters _dispatch_bridge_tool, which
    # //// checks the session scope and the tool's schema before any hook or dispatch —
    # //// exactly what the entry would have met alone. Any other local entry keeps
    # //// upstream's refusal (resolve_underlying_call never lets one through).
    from tools.tool_search import TOOL_CALL_NAME, is_mcp_tool_name

    if any(not is_mcp_tool_name((call or {}).get("name")) for _, call in partition.local):
        from tools.tool_search_validation import local_batch_error
        return tool_error(local_batch_error(calls))
    jobs = sorted([(plan.position, "remote", plan) for plan in partition.remote]  # //// #710
                  + [(position, "mcp", call) for position, call in partition.local], key=lambda job: job[0])
    entries = list(partition.errors)
    for offset, (position, kind, job) in enumerate(jobs):  # //// #710
        if is_interrupted():
            # Check before every entry so /stop prevents unstarted remote side effects.
            left = jobs[offset:]  # //// #710
            entries.extend(fill_remote_failure(
                [j for _, k, j in left if k == "remote"], "Stopped by the user before this call was made.",
                code="INTERRUPTED"))
            entries.extend({"index": p, "name": j.get("name"),  # //// #710
                            "error": {"code": "INTERRUPTED",
                                      "message": "Stopped by the user before this call was made."}}
                           for p, k, j in left if k == "mcp")
            break
        if kind == "remote":  # //// #710
            name, fn_name, fn_args = job.name, job.name, job.arguments
        else:
            name = job.get("name")
            fn_name = TOOL_CALL_NAME
            fn_args = {"calls": [{"name": name, "arguments": dict(job.get("arguments") or {})}]}
        # Each entry is its own call: its own policy, middleware, hooks and call id (``<id>#<position>``),
        # so observers never fold an entry into the wrapper or into a sibling entry.
        entry_ids = replace(ids, tool_call_id=f"{ids.tool_call_id}#{position}") if ids.tool_call_id else ids  # //// #710
        with suppress_post_tool_call_hook(False):
            payload = handle_function_call(
                fn_name, fn_args, **asdict(entry_ids), user_task=user_task,  # //// #710
                enabled_tools=enabled_tools, tool_request_middleware_trace=list(middleware_trace),
                skip_pre_tool_call_hook=False, skip_tool_request_middleware=False,
                skip_tool_execution_middleware=False,
                enabled_toolsets=enabled_toolsets, disabled_toolsets=disabled_toolsets,
            )
        try:
            value = json.loads(payload) if isinstance(payload, str) else payload
        except ValueError:
            value = payload
        entry = {"index": position, "name": name}  # //// #710
        if isinstance(value, dict) and "error" in value:
            error = value["error"]
            entry["error"] = error if isinstance(error, dict) else {"code": "TOOL_ERROR", "message": str(error)}
        else:
            entry["response"] = value.get("response", value) if isinstance(value, dict) else value
        if isinstance(value, dict) and value.get("account"):
            entry["account"] = value["account"]
        entries.append(entry)
    # //// END Neoffice ////
    return json.dumps(assemble_results(len(calls), entries), ensure_ascii=False)
