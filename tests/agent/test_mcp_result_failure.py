# //// Neoffice — added file (no upstream equivalent)
"""Argument rejection inside an MCP success envelope still counts as tool failure."""

import json

from agent.display import _detect_tool_failure
from agent.tool_guardrails import (
    ToolCallGuardrailConfig,
    ToolCallGuardrailController,
    classify_tool_failure,
)


TOOL = "mcp_neoffice_ventes_list_documents"
REJECTION = {
    "success": False,
    "error": "TypeError: list_documents() missing required argument: 'doctype'",
    "note": "Fix the arguments; do not repeat the identical call.",
}


def _envelope(value):
    # Shape emitted by tools.mcp_tool_handlers._render_call_tool_result.
    return json.dumps({"result": json.dumps(value)})


def test_mcp_status_is_bounded_and_does_not_scan_business_fields():
    from agent.tool_result_classification import mcp_result_failure

    nested = _envelope(REJECTION)
    success = {"success": True, "error": "a business field", "rows": [{"success": False}]}
    for value, expected in [
        (nested, True),
        ({"result": REJECTION}, True),
        ({"result": json.dumps(REJECTION), "_meta": {"trace": "fixture"}}, True),
        (_envelope(success), False),
        ({"result": success}, False),
        (_envelope({"success": False, "guardrail_refusal": True, "error": "Blocked"}), False),
    ]:
        assert mcp_result_failure(value) is expected
        assert _detect_tool_failure(TOOL, value)[0] is expected
        if isinstance(value, str):
            assert classify_tool_failure(TOOL, value)[0] is expected

    beyond_depth = {"result": {"result": {"result": {"result": REJECTION}}}}
    cycle = {}
    cycle["result"] = cycle
    for value in [
        None, "{", {"result": "{"}, {"result": '["error"]'},
        {"result": "[" * 2000 + "]" * 2000},
        {"result": json.dumps(REJECTION) + " " * 65_536},
        beyond_depth, cycle,
        {"success": True, "result": REJECTION},
        {"result": {"rows": [REJECTION]}},
    ]:
        assert mcp_result_failure(value) is None
    # Recognized status is required: truthy strings/numbers are not boolean false.
    for status in ["false", 0, None]:
        assert mcp_result_failure(_envelope({"success": status})) is None
    # Transport errors and non-MCP special cases keep their existing classification.
    for classifier in (_detect_tool_failure, classify_tool_failure):
        assert classifier(TOOL, '{"error":"MCP transport disconnected"}')[0] is True
        assert classifier(TOOL, '{"guardrail_refusal":true,"error":"Blocked"}')[0] is False
        assert classifier("terminal", '{"exit_code":0,"output":"error"}')[0] is False
        assert classifier("terminal", '{"exit_code":1}')[0] is True
        assert classifier("write_file", '{"bytes_written":12,"lint":{"error":"SyntaxError"}}')[0] is False


def test_wrapped_rejections_count_toward_existing_guard_without_changing_transport():
    config = ToolCallGuardrailConfig(hard_stop_enabled=True)
    args = {"query": "QA Customer Fictional"}
    # Exercise both executor classification and fallback classification, using real
    # controllers: the sixth identical rejection is blocked with default thresholds.
    for classify_explicitly in (True, False):
        controller = ToolCallGuardrailController(config)
        for index in range(config.exact_failure_block_after):
            assert controller.before_call(TOOL, args).action == "allow"
            payload = _envelope(REJECTION)
            kwargs = {"failed": _detect_tool_failure(TOOL, payload)[0]} if classify_explicitly else {}
            decision = controller.after_call(TOOL, args, payload, **kwargs)
            assert decision.count == index + 1
            assert decision.action == ("warn" if index + 1 >= config.exact_failure_warn_after else "allow")
        assert controller.before_call(TOOL, args).code == "repeated_exact_failure_block"
        # The raw envelope stays a success at the transport layer: no MCP handler,
        # server breaker, RPC or retry policy is modified by agent classification.
        assert set(json.loads(payload)) == {"result"}
        assert controller.before_call(TOOL, {"doctype": "Customer"}).action == "allow"

    controller = ToolCallGuardrailController(config)
    for _ in range(config.exact_failure_block_after + 1):
        assert controller.before_call(TOOL, args).action == "allow"
        controller.after_call(TOOL, args, _envelope({"success": True, "error": "business field"}))
    assert controller.before_call(TOOL, args).action == "allow"
