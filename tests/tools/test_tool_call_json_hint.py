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


# //// Neoffice — the closer repair (tools/tool_search_validation.py::_rebalance_closers). Shapes
# //// taken from the malformed `calls` strings workers really sent; the values are neutral.
import tools.tool_search_validation as validation

_EMAIL_ARGS = {
    "message": "Bonjour Madame Exemple,\n\nVotre commande partira lundi prochain.\n\nL'équipe",
    "recipient": "client@example.com",
    "subject": "Votre commande",
}


def _with_registered(names, test):
    """Run ``test`` with ``names`` answering as registered tools."""
    original = validation._registry_entry
    validation._registry_entry = lambda name: object() if name in names else original(name)
    try:
        test()
    finally:
        validation._registry_entry = original


def test_the_array_closed_before_its_call_object_is_repaired():
    # `}]}` for `}}]`: 21 of the 29 malformed strings, 14 of them one e-mail sent again and again.
    good = json.dumps([{"name": "mcp__example__send_email", "arguments": _EMAIL_ARGS}], ensure_ascii=False)
    assert good.endswith("}}]")
    broken = good[:-3] + "}]}"
    entries, err = normalize_tool_call_entries({"calls": broken})
    assert err is None
    assert entries == [{"name": "mcp__example__send_email", "arguments": _EMAIL_ARGS}]  # text untouched


def test_a_missing_array_closer_is_added():
    entries, err = normalize_tool_call_entries(
        {"calls": '[{"name": "mcp__example__balances", "arguments": {"params": {"days": 30}}}'})
    assert err is None
    assert entries == [{"name": "mcp__example__balances", "arguments": {"params": {"days": 30}}}]


def test_a_square_closer_written_for_a_curly_one_is_repaired():
    broken = ('[{"arguments": {"doctype": "Sales Invoice", "filters": "[[\\"customer\\", \\"like\\", '
              '\\"%Example%\\"]]"], "name": "mcp__example__list_documents"}]')
    entries, err = normalize_tool_call_entries({"calls": broken})
    assert err is None
    assert entries[0]["name"] == "mcp__example__list_documents"
    assert entries[0]["arguments"]["filters"] == '[["customer", "like", "%Example%"]]'


def test_a_tool_name_left_inside_arguments_is_lifted_out():
    broken = ('[{"arguments": {"doctype": "Sales Invoice", "limit": 10, '
              '"name": "mcp__example__list_documents"}] ')

    def check():
        entries, err = normalize_tool_call_entries({"calls": broken})
        assert err is None
        assert entries == [{"name": "mcp__example__list_documents",
                            "arguments": {"doctype": "Sales Invoice", "limit": 10}}]
    _with_registered({"mcp__example__list_documents"}, check)


def test_a_name_parameter_that_is_not_a_tool_stays_where_it_is():
    entries, err = normalize_tool_call_entries(
        {"calls": [{"arguments": {"name": "Example Customer", "party_type": "Customer"}}]})
    assert entries == []
    assert "requires a 'name'" in err


def test_a_cut_off_string_is_not_repaired():
    cut = '[{"name": "mcp__example__send_email", "arguments": {"subject": "Votre commande", "message": "Bonj'
    entries, err = normalize_tool_call_entries({"calls": cut})
    assert entries == [] and "not valid JSON" in err


def test_brackets_that_cannot_be_repaired_get_the_brackets_hint():
    # `arguments` closed before its last key: the parse breaks on a colon, the text values are whole.
    broken = ('[{"arguments": {"kind": "needs_input", "reason": "Quel article ?"}, "task_id": "t_1"}, '
              '"name": "kanban_block"}]')
    entries, err = normalize_tool_call_entries({"calls": broken})
    assert entries == []
    assert "brackets do not match" in err and "}}]" in err
    assert "escape a double quote" not in err


def test_a_broken_arguments_string_is_repaired_too():
    entries, err = normalize_tool_call_entries(
        {"calls": [{"name": "mcp__example__balances", "arguments": '{"params": {"days": 30}'}]})
    assert err is None
    assert entries == [{"name": "mcp__example__balances", "arguments": {"params": {"days": 30}}}]


def test_a_second_call_opened_inside_the_first_gets_the_brackets_hint():
    broken = ('[{"name": "kanban_complete", "arguments": {"summary": "Fait.", "task_id": "t_1"}, '
              '{"arguments": {"note": "x"}, "name": "kanban_comment"}]')
    entries, err = normalize_tool_call_entries({"calls": broken})
    assert entries == []
    assert "brackets do not match" in err


def test_a_raw_quote_in_a_filter_string_keeps_the_quote_hint():
    broken = ('[{"arguments": {"doctype": "Sales Invoice", "filters": "[[\\"customer\\", \\"like\\", '
              '"%Example%\\"]]", "limit": 20}, "name": "mcp__example__list_documents"}]')
    entries, err = normalize_tool_call_entries({"calls": broken})
    assert entries == []
    assert "escape a double quote" in err
