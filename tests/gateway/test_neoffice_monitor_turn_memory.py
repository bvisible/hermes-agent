# //// Neoffice — added file (no upstream equivalent): a monitoring turn leaves no trace in memory.
"""A monitoring probe's turn writes nothing to memory; every other turn writes as before.

An hourly health check asks the assistant, through the real chat route, to answer with one word;
its webhook payload says ``context.channel == "monitor"``. Each probe used to go through the
per-turn memory capture and was stored as the probing account's own memory, hundreds of copies
per instance.

The switch is a ContextVar (``agent.memory_manager.turn_memory_enabled``) set per inbound event.
These tests drive it through the real path: ``_hm_admit_event`` sets it in the message's task,
``_run_in_executor_with_context`` carries it into the agent's thread, and the agent's own
``_sync_external_memory_for_turn`` reaches ``MemoryManager.sync_all`` — whose provider must not
be called. A message queued while the session was busy runs as a follow-up in the previous
turn's task (``_run_agent_queued_followup``): it gets its own event's switch, and the previous
turn's switch comes back afterwards.
"""

from __future__ import annotations

import contextvars
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.memory_manager import (
    MemoryManager,
    reset_turn_memory_enabled,
    set_turn_memory_enabled,
    turn_memory_enabled,
)
from agent.memory_provider import MemoryProvider
from gateway.neoffice_turn_memory import is_monitoring_event


class _RecordingProvider(MemoryProvider):
    """Records every write and every queued recall."""

    def __init__(self):
        self.synced = []
        self.prefetched = []

    @property
    def name(self) -> str:
        return "recording"

    def initialize(self, session_id: str = "", **kwargs) -> None:
        pass

    def is_available(self) -> bool:
        return True

    def system_prompt_block(self) -> str:
        return ""

    def prefetch(self, query, *, session_id: str = "") -> str:
        return ""

    def queue_prefetch(self, query, *, session_id: str = "") -> None:
        self.prefetched.append(query)

    def sync_turn(self, user_content, assistant_content, *, session_id: str = "", messages=None) -> None:
        self.synced.append(user_content)

    def get_tool_schemas(self):
        return []

    def handle_tool_call(self, tool_name, args, **kwargs) -> str:
        return ""


def _manager():
    manager = MemoryManager()
    provider = _RecordingProvider()
    manager.add_provider(provider)
    return manager, provider


PROBE = "Answer with the single word PROBEOK."
QUESTION = "How many invoices are overdue this month?"


def _webhook_event(text: str, context):
    """A chat-route webhook event, shaped as the webhook adapter builds it (payload as raw_message)."""
    from gateway.platforms.base import MessageEvent, MessageType
    from gateway.session import SessionSource
    from gateway.config import Platform

    payload = {"message": text, "user": "probe@example.com"}
    if context is not None:
        payload["context"] = context
    source = SessionSource(
        platform=Platform.WEBHOOK, chat_id="webhook:nora_chat:user:probe@example.com",
        chat_type="webhook", user_id="probe@example.com", user_name="nora_chat")
    return MessageEvent(text=text, message_type=MessageType.TEXT, source=source,
                        raw_message=payload, message_id="delivery-1")


def _admitting_runner():
    """A bare GatewayRunner whose ingress gates let a webhook event through."""
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._scale_to_zero_note_real_inbound = lambda: None

    async def _passthrough_hook(event, source):
        return event

    runner._hm_pre_gateway_dispatch_hook = _passthrough_hook
    runner._is_user_authorized_for_source = lambda source, **kw: True
    runner._is_user_authorized = lambda source, **kw: True
    runner._admit_bot_message = lambda source: True
    return runner


async def _admit_and_end_the_turn(event):
    """Admit ``event`` as the gateway does, then end its turn in the agent's executor thread."""
    from gateway.run import GatewayRunner
    from run_agent import AIAgent

    manager, provider = _manager()
    runner = _admitting_runner()
    admitted = await runner._hm_admit_event(event)
    assert admitted is not None, "the bare runner must admit the event for the path to be real"

    def _end_of_turn():
        # What the agent does at the end of a turn, in the thread the gateway runs it in.
        agent = SimpleNamespace(_memory_manager=manager, session_id="s1", _turn_author=None)
        AIAgent._sync_external_memory_for_turn(
            agent, original_user_message=event.text, final_response="Done.", interrupted=False)

    await GatewayRunner._run_in_executor_with_context(runner, _end_of_turn)
    assert manager.flush_pending(timeout=10) is True
    manager.shutdown_all()
    return provider


# -- the event says what it is -------------------------------------------------------------


@pytest.mark.parametrize("context, expected", [
    ({"channel": "monitor"}, True),
    ({"channel": " Monitor "}, True),
    ({"channel": "desk"}, False),
    ({"channel": "nora-console-voice"}, False),
    ({}, False),
    (None, False),
    ("monitor", False),
])
def test_a_monitoring_event_is_named_by_its_payload_context(context, expected):
    assert is_monitoring_event(_webhook_event(PROBE, context)) is expected


def test_an_event_without_a_payload_is_not_a_probe():
    assert is_monitoring_event(SimpleNamespace(raw_message=None)) is False
    assert is_monitoring_event(None) is False


# -- the memory manager honours the switch --------------------------------------------------


def test_a_turn_without_memory_syncs_nothing_and_queues_no_recall():
    manager, provider = _manager()

    def _turn():
        set_turn_memory_enabled(False)
        manager.sync_all(PROBE, "PROBEOK", session_id="s1")
        manager.queue_prefetch_all(PROBE, session_id="s1")

    contextvars.copy_context().run(_turn)
    assert manager.flush_pending(timeout=10) is True
    manager.shutdown_all()
    assert provider.synced == [] and provider.prefetched == []


def test_a_normal_turn_still_syncs_and_queues_recall():
    manager, provider = _manager()
    manager.sync_all(QUESTION, "Twelve.", session_id="s1")
    manager.queue_prefetch_all(QUESTION, session_id="s1")
    assert manager.flush_pending(timeout=10) is True
    manager.shutdown_all()
    assert provider.synced == [QUESTION] and provider.prefetched == [QUESTION]


# -- through the real admission path, into the agent's thread -------------------------------


@pytest.mark.asyncio
async def test_a_monitoring_probe_writes_nothing_through_the_real_path():
    provider = await _admit_and_end_the_turn(_webhook_event(PROBE, {"channel": "monitor"}))
    assert provider.synced == [], "the probe reached the memory provider"
    assert provider.prefetched == []


@pytest.mark.asyncio
async def test_a_desk_turn_still_writes_through_the_real_path():
    provider = await _admit_and_end_the_turn(_webhook_event(QUESTION, {"channel": "desk"}))
    assert provider.synced == [QUESTION]


@pytest.mark.asyncio
async def test_a_turn_without_context_still_writes_through_the_real_path():
    provider = await _admit_and_end_the_turn(_webhook_event(QUESTION, None))
    assert provider.synced == [QUESTION]


@pytest.mark.asyncio
async def test_admission_overrides_a_switch_inherited_from_another_message():
    """A message's task is created with a copy of the spawning context: a switch left off by a
    probe must not follow a real message into its turn."""
    set_turn_memory_enabled(False)  # what a probe left in the context this task was copied from
    provider = await _admit_and_end_the_turn(_webhook_event(QUESTION, {"channel": "desk"}))
    assert provider.synced == [QUESTION]


# -- a follow-up queued while the session was busy -------------------------------------------


def _followup_runner(record):
    """A runner whose recursive ``_run_agent`` records the switch it runs under."""
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._MAX_INTERRUPT_DEPTH = 8

    async def _run_agent(**kwargs):
        record.append(turn_memory_enabled())
        return {"final_response": "done", "messages": []}

    runner._run_agent = _run_agent
    runner._run_agent_deliver_first_response = AsyncMock()
    runner._is_goal_continuation_event = MagicMock(return_value=False)
    runner._session_key_for_source = MagicMock(return_value="agent:main:webhook:dm:probe")
    runner._prepare_profile_scoped_inbound_message_text = AsyncMock(return_value="the follow-up")
    runner._reply_anchor_for_event = MagicMock(return_value=None)
    runner._delivery_adapter_for = MagicMock(return_value=None)
    runner._intake_adapter_for = MagicMock(return_value=None)
    runner._refresh_agent_cache_message_count = AsyncMock()
    return GatewayRunner, runner


def _turn_ctx(source):
    return SimpleNamespace(
        source=source, session_id="sid", session_key="agent:main:webhook:dm:probe", run_generation=1,
        _interrupt_depth=0, history=[], _status_thread_metadata=None,
        context_prompt=None, result_holder=[None])


def _pending(context):
    event = _webhook_event(PROBE if context else QUESTION, context)
    return SimpleNamespace(
        source=event.source, message_id="delivery-2", channel_prompt=None, message_type=None,
        internal=False, metadata={}, raw_message=event.raw_message, text=event.text)


@pytest.mark.asyncio
async def test_a_probe_queued_behind_a_real_turn_runs_without_memory_then_restores():
    record = []
    GatewayRunner, runner = _followup_runner(record)
    pending = _pending({"channel": "monitor"})
    token = set_turn_memory_enabled(True)  # the real turn that was running
    try:
        await GatewayRunner._run_agent_queued_followup(
            runner, _turn_ctx(pending.source), adapter=None, pending="again", pending_event=pending,
            response="resp", result={"interrupted": True, "messages": []}, stream_task=None)
        assert record == [False], "the queued probe ran with memory on"
        assert turn_memory_enabled() is True, "the outer turn's switch was not restored"
    finally:
        reset_turn_memory_enabled(token)


@pytest.mark.asyncio
async def test_a_real_message_queued_behind_a_probe_keeps_its_memory():
    record = []
    GatewayRunner, runner = _followup_runner(record)
    pending = _pending(None)
    token = set_turn_memory_enabled(False)  # the probe that was running
    try:
        await GatewayRunner._run_agent_queued_followup(
            runner, _turn_ctx(pending.source), adapter=None, pending="again", pending_event=pending,
            response="resp", result={"interrupted": True, "messages": []}, stream_task=None)
        assert record == [True], "a real message queued behind a probe lost its memory"
        assert turn_memory_enabled() is False
    finally:
        reset_turn_memory_enabled(token)
