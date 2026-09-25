# //// Neoffice — added file (no upstream equivalent): which inbound turns leave no trace in memory.
"""A turn that must not write to memory: a monitoring probe.

An hourly health check asks NORA, through the real chat route, to answer with one word. Each
probe went through the per-turn memory capture and was stored as the probing account's own
memory (hundreds of copies per instance). The probe says what it is in its webhook payload:
``context.channel == "monitor"``.

The switch lives in ``agent.memory_manager`` (a ContextVar read by ``sync_all`` and
``queue_prefetch_all``). It is set per inbound EVENT, at the two places a turn starts for one:

- ``_hm_admit_event``, the entry of every message's own task;
- ``_run_agent_queued_followup``, where a message queued while the session was busy runs as a
  follow-up in the PREVIOUS turn's task. Without this second place a real message queued behind a
  probe would inherit the probe's switch and lose its memory, and a probe queued behind a real
  message would be captured.
"""

from __future__ import annotations

from typing import Any, Optional

MONITOR_CHANNEL = "monitor"


def is_monitoring_event(event: Any) -> bool:
    """Whether ``event`` is a monitoring probe (its webhook payload's ``context.channel``)."""
    raw = getattr(event, "raw_message", None)
    context = raw.get("context") if isinstance(raw, dict) else None
    return isinstance(context, dict) and str(context.get("channel") or "").strip().lower() == MONITOR_CHANNEL


def bind_turn_memory(event: Any):
    """Set the memory switch for ``event``'s turn in the current context; returns a reset token."""
    from agent.memory_manager import set_turn_memory_enabled

    return set_turn_memory_enabled(not is_monitoring_event(event))


def unbind_turn_memory(token: Optional[Any]) -> None:
    """Restore the switch as it was before ``bind_turn_memory`` returned ``token``."""
    if token is None:
        return
    from agent.memory_manager import reset_turn_memory_enabled

    reset_turn_memory_enabled(token)
