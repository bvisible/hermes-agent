"""Shared helpers for classifying tool result payloads."""

from __future__ import annotations

import json
from typing import Any


FILE_MUTATING_TOOL_NAMES = frozenset({"write_file", "patch"})


# Tools whose interrupted/dangling execution is safe to discard because they
# cannot mutate either external state or Hermes session state. Unknown/plugin/
# MCP tools stay effect-capable by default.
NO_EFFECT_TOOL_NAMES = frozenset({
    "read_file", "search_files", "session_search", "skill_view", "skills_list",
    "web_extract", "web_search", "vision_analyze", "browser_snapshot",
    "browser_get_images", "browser_console", "read_terminal",
})


def tool_may_have_side_effect(tool_name: str) -> bool:
    return tool_name not in NO_EFFECT_TOOL_NAMES


# Set by a tool that REFUSED a call the harness judged redundant (repeated identical
# read/search). The body still carries ``"error"`` so the model reads it as a stop
# signal, but nothing failed: failure classifiers must not count it, or the cheap
# refusal feeds the streak that fires ``repeated_exact_failure_block``.
GUARDRAIL_REFUSAL_KEY = "guardrail_refusal"


# //// Neoffice — added function (no upstream equivalent): the explicit success/failure status an MCP
# //// server puts inside its JSON-in-``result`` envelope. Used by tool_guardrails and display.
def mcp_result_failure(result: Any) -> bool | None:
    """Classify explicit status inside MCP's JSON-in-``result`` envelope.

    Argument rejections can be successful MCP transports but failed tool calls.
    Only follow the renderer's envelope, never arbitrary business fields. Unknown
    or oversized payloads retain the caller's existing classification behavior.
    """
    data = result
    unwrapped = False
    for _ in range(4):  # Outer payload plus at most three result envelopes.
        if isinstance(data, str):
            if len(data) > 65_536:
                return None
            try:
                data = json.loads(data)
            except (ValueError, RecursionError):
                return None
        if not isinstance(data, dict):
            return None
        if unwrapped:
            if data.get(GUARDRAIL_REFUSAL_KEY) is True:
                return False
            if data.get("success") is True:
                return False
            if data.get("success") is False:
                return True
        if "result" not in data or not data.keys() <= {"result", "_meta", "structuredContent"}:
            return None
        data = data["result"]
        unwrapped = True
    return None
# //// END Neoffice ////


def is_guardrail_refusal(result: Any) -> bool:
    """Return True when ``result`` (JSON string or parsed dict) is a harness refusal."""
    data = result
    if isinstance(result, str):
        try:
            data = json.loads(result.strip())
        except Exception:
            return False
    return isinstance(data, dict) and data.get(GUARDRAIL_REFUSAL_KEY) is True


def file_mutation_result_landed(tool_name: str, result: Any) -> bool:
    """Return True when a file mutation result proves the write landed."""
    if tool_name not in FILE_MUTATING_TOOL_NAMES or not isinstance(result, str):
        return False
    try:
        data = json.loads(result.strip())
    except Exception:
        return False
    if not isinstance(data, dict) or data.get("error"):
        return False
    if tool_name == "write_file":
        return "bytes_written" in data
    if tool_name == "patch":
        return data.get("success") is True
    return False
