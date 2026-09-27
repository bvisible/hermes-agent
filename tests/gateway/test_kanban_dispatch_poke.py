"""//// Neoffice — added file (no upstream equivalent).

The kanban dispatcher ticks as soon as the chat router pokes it. The v2026.9.7 port kept
our poke-aware wait AND upstream's `_sleep_between_ticks`, so a poke was followed by one
more full interval before the tick (1.05-1.11 s measured on the dev instance, 2026-09-27).
"""
import asyncio
import time
import types

from gateway import kanban_watchers as kw


class _Runner(kw.GatewayKanbanWatchersMixin):
    def __init__(self):
        self._running = True

    def _kanban_dispatcher_boot(self):
        return (lambda: {}, types.SimpleNamespace(), {})

    def _release_kanban_dispatcher_lock(self):
        pass


def test_a_poke_cuts_the_wait_short(monkeypatch):
    monkeypatch.setattr(kw, "KANBAN_POKE", None)

    async def scenario():
        kw.KANBAN_POKE = asyncio.Event()
        asyncio.get_running_loop().call_later(0.05, kw.poke_kanban_dispatcher)
        t0 = time.monotonic()
        poked = await _Runner()._kanban_dispatch_wait(5.0)
        return poked, time.monotonic() - t0

    poked, waited = asyncio.run(scenario())
    assert poked is True
    assert waited < 0.5


def test_without_a_poke_the_wait_lasts_the_interval(monkeypatch):
    monkeypatch.setattr(kw, "KANBAN_POKE", None)

    async def scenario():
        kw.KANBAN_POKE = asyncio.Event()
        t0 = time.monotonic()
        poked = await _Runner()._kanban_dispatch_wait(1.0)
        return poked, time.monotonic() - t0

    poked, waited = asyncio.run(scenario())
    assert poked is False
    assert 0.9 <= waited < 1.5


def test_the_tick_after_a_poke_runs_at_once(monkeypatch):
    """The loop itself: a poke during the wait is followed by the tick, not by a sleep."""
    ticks: list[float] = []

    class _Dispatcher:
        def __init__(self, _kb, _settings):
            pass

        def tick_once(self):
            ticks.append(time.monotonic())
            return []

        def ready_nonempty(self):
            return False

        def auto_decompose_tick(self, _per_tick):
            pass

    async def _inline(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    real_sleep = asyncio.sleep

    async def _sleep(delay, *args, **kwargs):
        # The watcher's 5 s start-up delay is skipped; any other sleep is real.
        return await real_sleep(0 if delay >= 5 else delay, *args, **kwargs)

    from hermes_cli import kanban_db_dispatch

    monkeypatch.setattr(kw, "KANBAN_POKE", None)
    monkeypatch.setattr(kw, "_resolve_dispatcher_settings", lambda _cfg, _kb: types.SimpleNamespace(interval=1.0))
    monkeypatch.setattr(kw, "_KanbanDispatcher", _Dispatcher)
    monkeypatch.setattr(kw, "_to_thread_process_service", _inline)
    monkeypatch.setattr(kw, "_kanban_dispatch_allowed", lambda: True)
    monkeypatch.setattr(kw, "_resolve_auto_decompose_settings", lambda _load: (False, 0))
    monkeypatch.setattr(kw, "_log_spawn_results", lambda _results: False)
    monkeypatch.setattr(kanban_db_dispatch, "reap_worker_zombies", lambda: [])
    monkeypatch.setattr(kw.asyncio, "sleep", _sleep)

    async def scenario():
        runner = _Runner()
        watcher = asyncio.create_task(runner._kanban_dispatcher_watcher())
        while not ticks:
            await real_sleep(0.01)
        await real_sleep(0.2)  # inside the wait that follows the first tick
        poked_at = time.monotonic()
        kw.poke_kanban_dispatcher()
        while len(ticks) < 2:
            await real_sleep(0.01)
        runner._running = False
        await asyncio.wait_for(watcher, timeout=3.0)
        return ticks[1] - poked_at

    assert asyncio.run(scenario()) < 0.3
