"""//// Neoffice — added file (no upstream equivalent).

A kanban worker (dispatcher-spawned `chat -q`, HERMES_KANBAN_TASK set) starts its MCP
discovery in the background, so the pole's server starts while the worker imports and
builds its agent, and the agent build still waits for the server's tools.
"""
from __future__ import annotations

from argparse import Namespace
from contextlib import nullcontext
import logging
import sys
import threading
import time
import types

import pytest

from hermes_cli import main as main_mod
from hermes_cli import mcp_startup


@pytest.fixture(autouse=True)
def _reset_mcp_startup_state():
    saved_started = mcp_startup._mcp_discovery_started
    saved_thread = mcp_startup._mcp_discovery_thread
    try:
        mcp_startup._mcp_discovery_started = set()
        mcp_startup._mcp_discovery_thread = {}
        yield
    finally:
        thread = mcp_startup._current_home_thread()
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        mcp_startup._mcp_discovery_started = saved_started
        mcp_startup._mcp_discovery_thread = saved_thread


def _chat_args() -> Namespace:
    return Namespace(accept_hooks=False, command="chat", cron_command=None, gateway_command=None,
                     mcp_action=None, tui=False)


def test_a_kanban_worker_backgrounds_its_mcp_discovery(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test")
    assert main_mod._should_background_mcp_startup(_chat_args()) is True


def test_a_kanban_worker_waits_for_its_server_as_long_as_the_synchronous_path(monkeypatch):
    import hermes_cli.config as cfg

    monkeypatch.setattr(cfg, "load_config", lambda: {"mcp_single_query_discovery_timeout": 15.0})
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test")
    assert mcp_startup._resolve_discovery_timeout(None, single_query=True) == 60.0
    assert mcp_startup._resolve_discovery_timeout(5.0, single_query=True) == 5.0  # explicit wins
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    assert mcp_startup._resolve_discovery_timeout(None, single_query=True) == 15.0


def test_startup_returns_at_once_and_the_agent_build_gets_the_tools(monkeypatch):
    """The slow server no longer blocks start-up; the build still waits for its tools."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test")
    registered = threading.Event()

    def _slow_discover():
        time.sleep(0.5)  # the pole's MCP server starting
        registered.set()

    monkeypatch.setitem(sys.modules, "hermes_cli.plugins", types.SimpleNamespace(discover_plugins=lambda: None))
    monkeypatch.setitem(sys.modules, "hermes_cli.config", types.SimpleNamespace(
        read_raw_config=lambda: {"mcp_servers": {"neoffice-demo": {"transport": "stdio"}}},
        load_config=lambda: {}, DEFAULT_CONFIG={}))
    monkeypatch.setitem(sys.modules, "agent.shell_hooks",
                        types.SimpleNamespace(register_from_config=lambda *_a, **_k: None))
    monkeypatch.setitem(sys.modules, "agent.outbound_webhooks",
                        types.SimpleNamespace(register_from_config=lambda *_a, **_k: None))
    monkeypatch.setitem(sys.modules, "tools.mcp_oauth",
                        types.SimpleNamespace(suppress_interactive_oauth=lambda: nullcontext()))
    monkeypatch.setitem(sys.modules, "tools.mcp_tool_discovery", types.SimpleNamespace(
        discover_mcp_tools=_slow_discover, get_mcp_status=lambda: [{"connected": True}]))

    start = time.monotonic()
    main_mod._prepare_agent_startup(_chat_args())
    assert time.monotonic() - start < 0.3
    assert not registered.is_set()

    mcp_startup.ensure_mcp_discovery_before_agent_build(logger=logging.getLogger(__name__), single_query=True)
    assert registered.is_set()
