# //// Neoffice — added file (no upstream equivalent). A tool_call parse error says how to fix it.
"""A malformed ``tool_call`` says how to fix it, not only that it failed.

Capability bench of 2026-09-26: preparing an e-mail, a support worker passed ``calls`` as a
JSON STRING whose e-mail body held unescaped double quotes. The error said only
« 'calls' is not valid JSON: Expecting ',' delimiter », and the model re-sent the identical
string six times, until the loop guard. The error now names the array form, the escaping, and
tells the model not to resend the same string.
"""

import json

from tools.tool_search_validation import normalize_tool_call_entries

# The shape the worker sent: a stringified batch whose text value carries raw double quotes.
_BROKEN_CALLS = (
    '[{"name": "mcp__neoffice-support__send_email", "arguments": '
    '{"subject": "Votre demande", "content": "Il a dit "oui" hier."}}]'
)


def test_a_broken_calls_string_says_how_to_fix_it():
    entries, err = normalize_tool_call_entries({"calls": _BROKEN_CALLS})
    assert entries == []
    assert "not valid JSON" in err  # the upstream wording stays first
    assert "ARRAY of objects, not as a string" in err
    assert '\\"' in err and "«" in err
    assert "Do not resend the same string." in err


def test_broken_arguments_string_says_how_to_fix_it():
    entries, err = normalize_tool_call_entries(
        {"calls": [{"name": "send_email", "arguments": '{"content": "Il a dit "oui" hier."}'}]}
    )
    assert entries == []
    assert "calls[0].arguments is not valid JSON" in err
    assert "OBJECT, not as a string" in err
    assert "Do not resend the same string." in err


def test_the_fixed_forms_parse():
    fixed = [{"name": "send_email", "arguments": {"content": 'Il a dit "oui" hier.'}}]
    for args in ({"calls": fixed}, {"calls": json.dumps(fixed)}):
        entries, err = normalize_tool_call_entries(args)
        assert err is None
        assert entries == fixed
