"""A Hermes runtime var declared by a server's own config reaches that server.

A stdio MCP server gets a FILTERED environment (`_build_safe_env` keeps PATH,
HOME, USER, LANG, … and nothing else), so the server config's ``env:`` block is
its only channel for anything else. Those placeholders used to resolve from the
secret scope alone — and a runtime var is not a secret, so it resolved to
nothing and the literal ``${…}`` was kept.

Measured 2026-09-16: a kanban worker holds HERMES_KANBAN_TASK (the dispatcher
sets it; the worker's own log opens with the task id) but the MCP server it
spawns saw it empty. Every per-user tool then ran as the service account and the
model guessed a name — an hour of work was booked under the wrong person.

Resolving here rather than widening the safe-env allow-list keeps it opt-in:
only a server whose own config asks for the variable receives it.
"""

import os
from unittest import mock

from tools.mcp_tool_config import _interpolate_env_vars


def test_a_hermes_runtime_var_resolves_from_the_process_environment():
    with mock.patch.dict(os.environ, {"HERMES_KANBAN_TASK": "t_abc123"}, clear=False):
        assert _interpolate_env_vars("${HERMES_KANBAN_TASK}") == "t_abc123"


def test_it_resolves_inside_a_nested_config_block():
    """The env: block is a dict — the walk must reach it, not just top-level strings."""
    with mock.patch.dict(os.environ, {"HERMES_HOME": "/h/profiles/projet"}, clear=False):
        out = _interpolate_env_vars({"env": {"HERMES_HOME": "${HERMES_HOME}", "K": "v"}})
    assert out == {"env": {"HERMES_HOME": "/h/profiles/projet", "K": "v"}}


def test_a_non_hermes_var_still_keeps_its_placeholder():
    """The documented behaviour for an unset var is unchanged: this is a narrow
    fallback for Hermes' own runtime state, not a general os.environ passthrough
    that would hand a server any variable it happens to name."""
    os.environ.pop("SOME_UNSET_THING_XYZ", None)
    assert _interpolate_env_vars("${SOME_UNSET_THING_XYZ}") == "${SOME_UNSET_THING_XYZ}"


def test_an_absent_hermes_var_keeps_its_placeholder_too():
    """Absent stays absent — the fallback must not invent an empty string, which
    would read downstream as "resolved to nothing" instead of "never set"."""
    os.environ.pop("HERMES_NOT_SET_XYZ", None)
    assert _interpolate_env_vars("${HERMES_NOT_SET_XYZ}") == "${HERMES_NOT_SET_XYZ}"


def test_a_context_var_still_wins_over_the_fallback():
    """userHome is resolved by the context table, never by the environment."""
    assert _interpolate_env_vars("${userHome}") == os.path.expanduser("~")
