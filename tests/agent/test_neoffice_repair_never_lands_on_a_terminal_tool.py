# //// Neoffice — added file (no upstream equivalent): see _repairs_into_a_terminal_tool in agent/turn_tool_validation.py.
"""A fuzzy-repaired tool name never becomes kanban_complete or kanban_block (05.11).

The pole workers drop kanban tools they never call; KANBAN_GUIDANCE still names them, and the fuzzy repair would turn a stray
« kanban_comment » into kanban_complete and end the task.
"""

from difflib import get_close_matches

import pytest

from agent.turn_tool_validation import _repairs_into_a_terminal_tool

KEPT = ["kanban_show", "kanban_complete", "kanban_block", "skill_view", "skill_manage", "tool_search", "tool_describe", "tool_call"]


@pytest.mark.parametrize("asked", ["kanban_comment", "kanban_create", "kanban_link"])
def test_the_fuzzy_repair_would_end_the_task_and_is_refused(asked):
    repaired = get_close_matches(asked, KEPT, n=1, cutoff=0.7)[0]
    assert repaired in ("kanban_complete", "kanban_block")
    assert _repairs_into_a_terminal_tool(asked, repaired)


@pytest.mark.parametrize(
    "asked, repaired",
    [("KanbanComplete", "kanban_complete"), ("kanban-complete", "kanban_complete"), ("kanban_complete_tool", "kanban_complete"),
     ("Kanban Block", "kanban_block"), ("kanban_comment", "kanban_show"), ("tool_searh", "tool_search")],
)
def test_a_case_or_separator_repair_still_reaches_its_tool(asked, repaired):
    assert not _repairs_into_a_terminal_tool(asked, repaired)
