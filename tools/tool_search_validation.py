"""Local argument validation for ``tool_call`` against a deferred tool's schema."""

from __future__ import annotations

import copy
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from tools.registry import tool_error
from tools.tool_search_catalog import BRIDGE_TOOL_NAMES, _registry_entry

logger = logging.getLogger("tools.tool_search")

_SCHEMA_LITERAL_KEYS = frozenset({"const", "default", "enum", "example", "examples"})


def _schema_for_local_validation(node: Any) -> Any:
    """JSON-Schema-compatible copy honoring OpenAPI ``nullable: true`` (the normal coercion
    path accepts that shape, so local validation must too)."""
    if isinstance(node, list):
        return [_schema_for_local_validation(item) for item in node]
    if not isinstance(node, dict):
        return node
    # Literal keywords hold instance data, not schemas: copy byte-for-byte.
    normalized = {key: (copy.deepcopy(value) if key in _SCHEMA_LITERAL_KEYS
                        else _schema_for_local_validation(value))
                  for key, value in node.items() if key != "nullable"}
    if node.get("nullable") is not True:
        return normalized
    schema_type = normalized.get("type")
    if isinstance(schema_type, str):
        schema_type = [schema_type]
    if isinstance(schema_type, list):
        if "null" not in schema_type:
            normalized["type"] = [*schema_type, "null"]
        return normalized
    # No ``type`` to extend ($ref/combinator): wrap so local refs still resolve from the
    # root while null stays an explicit alternative.
    return {"anyOf": [normalized, {"type": "null"}]}


def _schema_has_external_ref(node: Any) -> bool:
    """True when *node* contains a non-local ``$ref`` — local validation must never turn a
    tool call into an implicit network/file fetch (fail open)."""
    if isinstance(node, list):
        return any(_schema_has_external_ref(item) for item in node)
    if not isinstance(node, dict):
        return False
    ref = node.get("$ref")
    return (isinstance(ref, str) and not ref.startswith("#")) or any(
        _schema_has_external_ref(value) for key, value in node.items()
        if key not in _SCHEMA_LITERAL_KEYS)


def _validation_path(error: Any) -> str:
    """Format a jsonschema error path as a compact argument path."""
    path = "arguments"
    for part in getattr(error, "absolute_path", ()):
        if isinstance(part, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", part):
            path += f".{part}"
        else:
            path += f"[{part if isinstance(part, int) else json.dumps(part, ensure_ascii=False)}]"
    return path


def _validation_error(message: str, *, path: str, constraint: str, parameters: Any) -> str:
    return tool_error(
        message, path=path, constraint=constraint, parameters=parameters,
        hint="Retry tool_call with 'arguments' matching the parameters schema above.")


def validate_deferred_call_args(name: str, args: Dict[str, Any]) -> Optional[str]:
    """Validate ``tool_call`` arguments against the deferred tool's schema. Models invoke
    deferred tools "blind" (schema unseen) and omit required args; without this, the opaque
    downstream failure makes cheap models loop. Required-field probe first, then the same
    schema-guided coercion normal dispatch applies, then jsonschema on the repaired copy.
    Missing/malformed schemas, no validator, and external refs all fail OPEN. Returns a JSON
    error string when invalid, ``None`` when the call should dispatch.

    This restores the concrete-schema checks that the provider cannot perform through the generic
    ``arguments: object`` bridge. See #5149.
    """
    try:
        from tools.registry import registry as _registry
        schema = _registry.get_schema(name)
        if not isinstance(schema, dict):
            return None
        fn = schema.get("function") if schema.get("type") == "function" else schema
        params = fn.get("parameters") if isinstance(fn, dict) else None
        if not isinstance(params, dict):
            return None
        required = params.get("required")
        missing = ([r for r in required if isinstance(r, str) and r not in args]
                   if isinstance(required, list) else [])
        if missing:
            return _validation_error(
                f"tool_call to '{name}' is missing required argument(s): "
                f"{', '.join(missing)}. The tool was NOT invoked.",
                path="arguments", constraint="required", parameters=params)
        validation_schema = _schema_for_local_validation(params)
        if _schema_has_external_ref(validation_schema):
            logger.debug("Skipping local deferred-argument validation for %s: external $ref", name)
            return None
        # Validate the repaired shape dispatch will see; copy because coerce_tool_args may
        # normalize in place (dispatch re-coerces canonically).
        try:
            from model_tools import coerce_tool_args
            candidate_args = coerce_tool_args(name, dict(args))
        except Exception:
            logger.debug("Deferred-argument coercion failed for %s", name, exc_info=True)
            candidate_args = dict(args)
        try:
            from jsonschema.exceptions import best_match
            from jsonschema.validators import validator_for
        except ImportError:
            logger.debug("jsonschema unavailable; keeping required-only validation for %s", name)
            return None
        validator_cls = validator_for(validation_schema)
        validator_cls.check_schema(validation_schema)
        validation_error = best_match(validator_cls(validation_schema).iter_errors(candidate_args))
        if validation_error is None:
            return None
        path = _validation_path(validation_error)
        constraint = str(getattr(validation_error, "validator", None) or "schema")
        detail = re.sub(r"\s+", " ", str(validation_error.message)).strip()
        if len(detail) > 600:
            detail = detail[:597] + "..."
        return _validation_error(
            f"tool_call to '{name}' failed argument validation at {path} "
            f"({constraint}): {detail}. The tool was NOT invoked.",
            path=path, constraint=constraint, parameters=params)
    except Exception:  # pragma: no cover — never block dispatch on validator bugs
        logger.debug("validate_deferred_call_args failed for %s", name, exc_info=True)
        return None


# //// Neoffice — the fix a model needs after a JSON parse error, not only the error (see
# //// normalize_tool_call_entries). Plain ASCII quotes inside a text value break a JSON string;
# //// « » need no escaping.
_CALLS_AS_ARRAY_HINT = (
    'Send `calls` as an ARRAY of objects, not as a string: {"calls": [{"name": "<tool>", '
    '"arguments": {...}}]}. Inside a text value, escape a double quote as \\" or use « ». '
    "Do not resend the same string.")
_ARGUMENTS_AS_OBJECT_HINT = (
    "Send `arguments` as an OBJECT, not as a string. Inside a text value, escape a double quote "
    "as \\\" or use « ». Do not resend the same string.")
# //// A parse that breaks on a closer, a colon or a comma (or finds extra data) means the text
# //// values came out whole and the brackets are wrong: say that, not the quote advice, which sent
# //// a model stripping the accents and line breaks out of an e-mail eight times on 2026-09-26.
_CALLS_CLOSERS_HINT = (
    "The brackets do not match; the text values are fine, keep them exactly as they are. One call "
    'is {"name": "<tool>", "arguments": {...}}: close `arguments` with }, the call with }, then '
    "the array with ], so a single call ends with }}]. Do not resend the same string.")
_ARGUMENTS_CLOSERS_HINT = (
    "The brackets do not match; the text values are fine, keep them exactly as they are. Close "
    "each { with } and each [ with ], innermost first. Do not resend the same string.")


def _json_fix_hint(text: str, error: json.JSONDecodeError, closers_hint: str, quote_hint: str) -> str:
    """The fix to suggest for ``error``: the brackets when the parse broke on a bracket, a colon or
    a comma (the text values came out whole), the quotes otherwise (a raw " ends a text value early
    and the parse stops on the word after it)."""
    at = text[error.pos:error.pos + 1]
    if error.msg.startswith("Extra data") or at in ("}", "]", "{", "[", ":", ","):
        return closers_hint
    return quote_hint


# //// A malformed `calls` string is repaired when only its closing brackets are wrong. Of the 43
# //// malformed `calls` strings our workers sent up to 2026-09-26 (test instance, all poles), 35
# //// had only their closers wrong (`}]}` for `}}]`, the array closed before its call object; a
# //// missing final `]`; a `]` for a `}`; `"name"` left inside `arguments`) and all 35 parse once
# //// repaired; 5 had a raw quote, 3 a closer too early. Only structural closers move, no text
# //// value is touched, and the repaired call still goes through the tool's own argument
# //// validation (validate_deferred_call_args).
_OPENER_OF = {"}": "{", "]": "["}
_CLOSER_OF = {"{": "}", "[": "]"}


def _rebalance_closers(text: str, *, substitute: bool) -> str:
    """Rebuild the closing brackets of ``text`` from its opening ones, text values untouched.

    A closer that does not match the innermost open bracket either first closes the brackets
    opened after its own (``substitute=False``: ``}]}`` becomes ``}}]``) or stands for the
    innermost bracket's closer (``substitute=True``: ``"x"]`` becomes ``"x"}``). A closer with
    nothing to close is dropped; brackets still open at the end are closed.
    """
    out: List[str] = []
    stack: List[str] = []
    in_string = escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if stack and stack[-1] != _OPENER_OF[ch]:
                if substitute:
                    ch = _CLOSER_OF[stack[-1]]
                elif _OPENER_OF[ch] in stack:
                    while stack[-1] != _OPENER_OF[ch]:
                        out.append(_CLOSER_OF[stack.pop()])
                else:
                    continue
            if not stack:
                continue
            stack.pop()
        out.append(ch)
    out.extend(_CLOSER_OF[bracket] for bracket in reversed(stack))
    return "".join(out)


def _loads_repairing_closers(text: str, accept) -> Any:
    """``json.loads(text)``; on a parse error, the first closer repair whose value ``accept``s.
    Re-raises the original error when no repair gives an accepted value. A text that does not end
    on a closer is never repaired: that is a cut-off output, and closing it would dispatch a call
    missing what the model had not written yet."""
    try:
        return json.loads(text)
    except json.JSONDecodeError as original:
        if text.rstrip()[-1:] not in ("}", "]"):
            raise
        for substitute in (False, True):
            candidate = _rebalance_closers(text, substitute=substitute)
            if candidate == text:
                continue
            try:
                value = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if accept(value):
                logger.info("tool_call: repaired the brackets of a malformed JSON string")
                return value
        raise original


def _is_tool_name(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    from tools.tool_search import _core_tool_names  # late: tool_search imports this module
    return value in _core_tool_names() or _registry_entry(value) is not None


def _lift_name_out_of_arguments(raw: Any) -> Any:
    """``{"arguments": {..., "name": "<tool>"}}`` → ``{"name": "<tool>", "arguments": {...}}``: the
    closer of ``arguments`` came after ``name``. Only when the entry has no name of its own and the
    value is a registered tool, so a tool's own ``name`` parameter is never taken for one."""
    if not isinstance(raw, dict) or str(raw.get("name") or "").strip():
        return raw
    arguments = raw.get("arguments")
    if not isinstance(arguments, dict) or not _is_tool_name(arguments.get("name")):
        return raw
    return {"name": arguments["name"], "arguments": {k: v for k, v in arguments.items() if k != "name"}}


def _looks_like_calls(value: Any) -> bool:
    items = value if isinstance(value, list) else [value]
    return bool(items) and all(
        isinstance(item, dict)
        and (str(item.get("name") or "").strip() or _lift_name_out_of_arguments(item) is not item)
        for item in items)
# //// END Neoffice ////


def normalize_tool_call_entries(args: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Normalize ``tool_call`` arguments into a ``calls[]`` list of entries.

    Accepts the advertised batch shape ``{"calls": [{"name", "arguments"}, ...]}``
    and, tolerantly, the legacy single shape ``{"name": ..., "arguments": ...}``
    (a single call is a batch of one). Each entry's ``arguments`` is coerced to
    a dict (JSON strings parsed, ``None`` → ``{}``). Returns ``(entries, None)``
    or ``([], error_message)``.
    """
    raw_calls = args.get("calls")
    if raw_calls is None:
        # Legacy single shape.
        if not str(args.get("name") or "").strip():
            return [], "tool_call requires 'calls' (an array of {name, arguments})"
        raw_calls = [{"name": args.get("name"), "arguments": args.get("arguments")}]
    if isinstance(raw_calls, str):
        # Tolerate the model emitting the batch envelope as a JSON string —
        # mirror the per-entry `arguments` handling below (#114484).
        # //// Neoffice — repair the brackets before giving up, and say how to fix it (upstream:
        # //// json.loads and the parse error alone). Told only « not valid JSON », a model re-sent
        # //// the identical string until the loop guard (see _rebalance_closers).
        try:
            raw_calls = _loads_repairing_closers(raw_calls, _looks_like_calls)
        except json.JSONDecodeError as e:
            hint = _json_fix_hint(raw_calls, e, _CALLS_CLOSERS_HINT, _CALLS_AS_ARRAY_HINT)
            return [], f"tool_call 'calls' is not valid JSON: {e}. {hint}"
        # //// END Neoffice ////
    if isinstance(raw_calls, dict):
        raw_calls = [raw_calls]
    if not isinstance(raw_calls, list) or not raw_calls:
        return [], "tool_call 'calls' must be a non-empty array of {name, arguments}"

    entries: List[Dict[str, Any]] = []
    for position, raw in enumerate(raw_calls):
        if not isinstance(raw, dict):
            return [], f"tool_call calls[{position}] must be an object with 'name' and 'arguments'"
        raw = _lift_name_out_of_arguments(raw)  # //// Neoffice — see _lift_name_out_of_arguments
        name = str(raw.get("name") or "").strip()
        if not name:
            return [], f"tool_call calls[{position}] requires a 'name'"
        if name in BRIDGE_TOOL_NAMES:
            return [], f"tool_call cannot invoke '{name}' (it is itself a bridge tool)"
        raw_args = raw.get("arguments")
        if raw_args is None or (isinstance(raw_args, str) and not raw_args.strip()):
            # "" / whitespace is how some OpenAI-compatible gateways spell "no arguments" for a
            # parameterless tool (#83937); the loop already treats an empty outer arguments string
            # as {} (turn_tool_validation), and a missing required param still surfaces below via
            # validate_deferred_call_args instead of an opaque JSON parse error.
            raw_args = {}
        if isinstance(raw_args, str):
            # //// Neoffice — same as the `calls` string above: repair the brackets, say how to fix it.
            try:
                raw_args = _loads_repairing_closers(raw_args, lambda value: isinstance(value, dict))
            except json.JSONDecodeError as e:
                hint = _json_fix_hint(raw_args, e, _ARGUMENTS_CLOSERS_HINT, _ARGUMENTS_AS_OBJECT_HINT)
                return [], f"tool_call calls[{position}].arguments is not valid JSON: {e}. {hint}"
            # //// END Neoffice ////
        if not isinstance(raw_args, dict):
            return [], f"tool_call calls[{position}].arguments must be an object"
        entries.append({"name": name, "arguments": raw_args})
    return entries, None


_ECHO_ARGS_MAX_CHARS = 1500


def local_batch_error(entries: List[Dict[str, Any]]) -> str:
    """Rejection for a multi-entry batch that names a local tool. Restates the valid
    shape with the caller's OWN first entry: small models re-send an identical batch
    when told only the constraint, and the echoed payload is what gets them unstuck."""
    first = entries[0]
    args = json.dumps(first.get("arguments", {}), ensure_ascii=False, separators=(",", ":"))
    if len(args) > _ECHO_ARGS_MAX_CHARS:
        args = "{...}"  # keep the correction readable; the model still has its own arguments
    retry = '{"calls":[{"name":%s,"arguments":%s}]}' % (json.dumps(first["name"], ensure_ascii=False), args)
    remaining = (f" then issue the remaining {len(entries) - 1} call(s) as separate tool_call invocations"
                 if len(entries) > 1 else "")
    return (
        f"tool_call takes exactly one entry for local tools; you sent {len(entries)}. "
        f"Retry with only: {retry}{remaining}. Only connectors__ names may be batched together."
    )


def not_deferrable_error(name: str) -> str:
    """Rejection for a ``tool_call`` naming something that is not a deferred tool.
    Two different mistakes reach here and need opposite corrections: a directly-listed
    tool (call it without the bridge) vs. an unknown name — typically a deferred MCP tool
    cited by its bare suffix instead of the full ``mcp__<server>__<tool>`` name. Telling
    the second group 'call it directly' is the opposite of what they must do."""
    from tools.tool_search import _core_tool_names  # late: tool_search imports this module
    if name in _core_tool_names() or _registry_entry(name) is not None:
        return (f"'{name}' is a directly-listed tool, not a deferred one. "
                "Call it directly instead of via tool_call.")
    suffix = f"__{name}"
    try:
        from tools.registry import registry
        candidates = sorted(n for n in registry.get_all_tool_names() if n.endswith(suffix))
    except Exception:
        candidates = []
    hint = (f" Did you mean {', '.join(repr(c) for c in candidates)}?" if candidates
            else " Use tool_search to find the exact name.")
    return (f"'{name}' is not a known tool name. Deferred tools must be invoked through tool_call "
            f"by the exact name tool_search returns (e.g. mcp__<server>__<tool>).{hint}")
