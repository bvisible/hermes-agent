"""//// Neoffice — added file (no upstream equivalent).

The local-toolchain probe runs only for an agent that can run commands: its subprocesses held up
every kanban worker's first model request for a line that worker could not use.
"""
import types

import pytest

from agent import agent_init


def _agent(tool_names):
    return types.SimpleNamespace(valid_tool_names=set(tool_names), run_budget_seconds=None)


@pytest.fixture(autouse=True)
def _no_probe_thread(monkeypatch):
    import tools.env_probe as probe

    started = []
    monkeypatch.setattr(probe, "warm_environment_probe_async", lambda: started.append(True))
    return started


def test_no_command_tool_no_probe(_no_probe_thread):
    agent = _agent({"kanban_show", "kanban_complete", "mcp__neoffice_ventes__get_document"})
    agent_init._apply_agent_section(agent, {})
    assert agent._environment_probe is False
    assert _no_probe_thread == []


@pytest.mark.parametrize("tool", ["terminal", "process_manage", "execute_code"])
def test_a_command_tool_keeps_the_probe(_no_probe_thread, tool):
    agent = _agent({"kanban_show", tool})
    agent_init._apply_agent_section(agent, {})
    assert agent._environment_probe is True
    assert _no_probe_thread == [True]


def test_the_config_switch_still_wins(_no_probe_thread):
    agent = _agent({"terminal"})
    agent_init._apply_agent_section(agent, {"agent": {"environment_probe": False}})
    assert agent._environment_probe is False
