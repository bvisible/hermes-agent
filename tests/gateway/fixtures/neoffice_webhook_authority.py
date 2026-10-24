# //// Neoffice — added file (no upstream equivalent): our conversational webhook routes behind a REAL authority.
"""A webhook adapter on a real ``SessionAuthority``, for the tests of NORA's conversational routes.

Since the durable queue (v0.22.3) a webhook message is committed by ``WebhookAdapter._admit_agent_run``
into the authority's admission FIFO, executed by its drain, then finalized. Everything here is the
real pipeline: our adapter code, the admission and its target session, the settlement and the
finalization. Only the turn itself is replaced (``execute_finite_admission``, the seam upstream's own
admission tests stub): ``turn(event, ref, history)`` runs instead of the agent, with the committed
event restored as the drain restores it and the transcript of the target session.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace


def all_rows(authority) -> list:
    """Every admission of every session the authority holds."""
    from hermes_state_runtime import list_session_admissions
    return [row for sid in list(authority.sessions)
            for row in list_session_admissions(authority.db, session_id=sid, pending_only=False)]


async def start(monkeypatch, routes: dict, turn) -> SimpleNamespace:
    """The adapter (with *routes*) on a fresh default-profile authority; *turn* is awaited for every turn."""
    import gateway.session_finite as finite
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.platforms.webhook import WebhookAdapter
    from gateway.run import GatewayRunner
    from gateway.session import SessionStore
    from gateway.session_authority import initialize_session_authority
    from gateway.session_envelope import restore_native
    from hermes_constants import get_hermes_home
    from hermes_state import SessionDB

    runner = GatewayRunner(GatewayConfig(platforms={Platform.WEBHOOK: PlatformConfig(enabled=True)}))
    # One default-profile authority, as upstream's route tests mount it.
    runner.config.multiplex_profiles = False
    runner.session_store = SessionStore(sessions_dir=get_hermes_home() / "sessions", config=runner.config)
    runner.session_store._db = SessionDB(db_path=get_hermes_home() / "state.db")
    runner._session_db = runner.session_store._db
    authority = await initialize_session_authority(runner, profile_id="default", instance_id="neoffice-route-test")
    adapter = WebhookAdapter(PlatformConfig(enabled=True, extra={"host": "127.0.0.1", "port": 0,
                                                                 "routes": dict(routes)}))
    adapter.gateway_runner = runner
    runner.adapters[Platform.WEBHOOK] = adapter
    runner._wire_adapter_handlers(adapter)
    store = runner.session_store

    async def produce(authority_, ref, row):
        event = restore_native(row["payload"], runner)
        return await turn(event, ref, store.load_transcript(ref.session_id)) or ""

    monkeypatch.setattr(finite, "execute_finite_admission", produce)
    return SimpleNamespace(adapter=adapter, store=store, authority=authority, runner=runner)


async def stop(h: SimpleNamespace) -> None:
    for task in list(h.adapter._background_tasks):
        task.cancel()
    await asyncio.gather(*h.adapter._background_tasks, return_exceptions=True)
    h.authority.db.close()


async def settled(adapter) -> None:
    """Every admission terminal and every finalizer done."""
    from tests.waiting import async_wait_until
    authority = adapter.gateway_runner.session_authority
    await async_wait_until(lambda: all(row["status"] == "terminal" for row in all_rows(authority)),
                           timeout=10, what="the admissions to settle")
    await async_wait_until(lambda: not adapter._background_tasks, timeout=10, what="the finalizers to finish")


async def admit(adapter, payload: dict, message: str, delivery_id: str, route_name: str, route_config: dict) -> str:
    """One inbound POST as ``_handle_webhook`` dispatches it, without waiting for its turn."""
    outcome = await adapter._admit_agent_run(payload, message, delivery_id, time.time(), route_config=route_config,
                                             route_name=route_name, profile=None, event_type="chat")
    assert outcome == "accepted", outcome
    return outcome
