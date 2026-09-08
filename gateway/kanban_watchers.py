"""Kanban board watcher methods for GatewayRunner.

Background loops that subscribe to kanban boards, deliver notifications and
artifacts, and drive the multi-agent dispatcher. They use only ``self`` state,
so they live on a mixin ``GatewayRunner`` inherits. Per-tick work lives in
``kanban_watchers_notifier`` / ``kanban_watchers_dispatcher``; shared plumbing
in ``kanban_watchers_common``.
"""

from __future__ import annotations

import asyncio
import os
# //// Neoffice — needed by the accounting-block guard and the gave_up rule below.
import re
# //// END Neoffice ////
import time
from pathlib import Path
from typing import Any, Optional

from gateway.kanban_watchers_common import (
    _acquire_singleton_lock,
    _kanban_dispatch_allowed,
    _release_singleton_lock,
    _resolve_auto_decompose_settings,
    _gc_retention_days,
    _to_thread_process_service,
    logger,
)
from gateway.kanban_watchers_notifier import _KanbanNotification, _notifier_collect
from gateway.kanban_watchers_dispatcher import (
    _KanbanDispatcher,
    _log_spawn_results,
    _resolve_dispatcher_settings,
)

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
_VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".3gp"}
_GC_INTERVAL_SECONDS = 3600.0
_HEALTH_WINDOW = 6


# //// Neoffice — event-driven dispatch poke. The dispatcher used to discover a
# new task only at its NEXT periodic tick (0..interval seconds of pure waiting;
# measured 3 s on a live run — the single biggest e2e variance). The chat
# router runs in the SAME process/loop, so it can wake the dispatcher the
# instant it creates a task. Best-effort by design: without the event the
# periodic tick still guarantees progress.
KANBAN_POKE: "asyncio.Event | None" = None


def poke_kanban_dispatcher() -> None:
    """Wake the dispatcher loop now (same-loop callers only; best-effort)."""
    ev = KANBAN_POKE
    if ev is not None:
        try:
            ev.set()
        except Exception:
            pass
# //// END Neoffice ////


# //// Neoffice — absolute paths must never reach a customer's chat: a block reason is
# //// written by a worker and can quote a workspace path. Redacted by
# //// _safe_accounting_block_reason below. Was lost when the v2026.9.7 port moved the
# //// guard's neighbours; pyflakes caught the undefined name, compilation did not.
_LOCAL_PATH_RE = re.compile(
    r"(?<![\w:/])(?:/(?:Users|home|private|tmp|var|etc|workspace)/[^\s,;]+|"
    r"[A-Za-z]:\\[^\s,;]+)"
)
# //// END Neoffice ////


# //// Neoffice — added. The `gave_up` event carries two very different situations and
# telling them apart inline got it wrong: a tripped circuit breaker (three crashed runs)
# sets the task to `blocked` and emits `gave_up`, and the old condition
# `status != "gave_up"` skipped exactly that — the desk stayed silent after "je transmets
# votre demande à votre pôle" and the user never learned the request had died
# (tracker #246). A pure function so the rule is testable instead of re-derived.
def _gave_up_delivery(status: str, result: str, payload: dict | None) -> str | None:
    """What to tell the user about a ``gave_up`` event — or nothing.

    Returns ``"breaker"`` (auto-blocked after repeated failures: the request ends
    here and the user must be told), ``"give_up"`` (the worker genuinely gave up),
    or ``None`` to stay silent because something else already speaks for this task.
    """
    payload = payload or {}
    if (result or "").strip():
        # The worker produced a real outcome; its own event delivered it.
        return None
    if "protocol violation" in str(payload.get("error", "")).lower():
        # Internal protocol noise — the task is retried, nothing to announce.
        return None
    status = (status or "").strip()
    if status == "blocked" and payload.get("failures"):
        return "breaker"
    if status == "gave_up":
        return "give_up"
    # ready / review / running: a retry is in flight, its outcome will speak.
    return None


def _safe_review_reason(value: Any, limit: int = 160) -> str:
    """Return a mobile-friendly review reason safe for external delivery."""
    from agent.redact import redact_sensitive_text

    reason = redact_sensitive_text(
        "" if value is None else str(value),
        force=True,
        redact_url_credentials=True,
    )
    reason = _LOCAL_PATH_RE.sub("[local path]", reason)
    reason = " ".join(reason.split())
    if len(reason) > limit:
        reason = reason[: limit - 1].rstrip() + "…"
    return reason

# //// Neoffice — deterministic anti-hallucination guard for blocked Swiss-
# accounting allocation questions. A worker may correctly block for a missing
# fact yet still leak speculative account numbers in the explanation around its
# question. The user-facing notifier keeps only explicit, number-free questions
# for this narrow class of task; the complete raw reason remains in Kanban for
# auditors. This is a delivery guard, never an accounting decision engine.
_ACCOUNT_ALLOCATION_MARKERS = (
    "imput", "quel compte", "dans quel compte", "compte comptable",
    "numéro de compte", "numero de compte", "account allocation",
)
_ACCOUNT_NUMBER_RE = re.compile(r"(?<!\d)\d{3,4}(?:[.,]\d+)?(?!\d)")
_QUESTION_FRAGMENT_RE = re.compile(r"(?:^|(?<=[.!?])\s+|\n+)([^\n.!?]*\?)")
_ACCOUNTING_BLOCK_FALLBACK = (
    "Quelle information factuelle manque-t-il pour départager les traitements "
    "comptables possibles ?"
)
_ACTIVATION_CLARIFICATION = (
    "Quel est le montant de l'achat et quelle politique ou quel seuil "
    "d'activation votre entreprise applique-t-elle à ce type de matériel ?"
)


def _safe_accounting_block_reason(
    reason: str,
    *,
    assignee: str | None,
    title: str = "",
    body: str = "",
) -> str:
    """Remove speculative account proposals from a blocked delivery.

    The rule is deliberately narrow: only the ``compta`` profile and tasks
    explicitly about choosing an account are filtered. Other blocked reasons
    (including legal references, ticket numbers and dates) are unchanged.
    """
    cleaned = str(reason or "").strip()
    context = f"{title}\n{body}".lower()
    if assignee != "compta" or not any(
        marker in context for marker in _ACCOUNT_ALLOCATION_MARKERS
    ):
        return cleaned[:600]

    questions = []
    for match in _QUESTION_FRAGMENT_RE.finditer(cleaned):
        question = " ".join(match.group(1).split())
        if question and not _ACCOUNT_NUMBER_RE.search(question):
            questions.append(question)
    if not questions:
        ambiguity = f"{context}\n{cleaned.lower()}"
        if "activation" in ambiguity and any(
            marker in ambiguity for marker in ("montant", "seuil", "immobil", "ordinateur")
        ):
            return _ACTIVATION_CLARIFICATION
        return _ACCOUNTING_BLOCK_FALLBACK
    return " ".join(questions)[:600]
# //// END Neoffice ////



class GatewayKanbanWatchersMixin:
    """Kanban watcher / notifier / dispatcher loops for GatewayRunner."""

    def _owns_kanban_dispatcher_lock(self) -> bool:
        return getattr(self, "_kanban_dispatcher_lock_handle", None) is not None

    def _release_kanban_dispatcher_lock(self) -> None:
        """Clear notifier-visible ownership before releasing the OS lock."""
        handle = getattr(self, "_kanban_dispatcher_lock_handle", None)
        self._kanban_dispatcher_lock_handle = None
        _release_singleton_lock(handle)

    async def _sleep_between_ticks(self, interval: float) -> None:
        """Sleep *interval* (floored to 1s) in 1s slices so stop() never waits a full interval."""
        interval = max(interval, 1.0)
        slept = 0.0
        while slept < interval and self._running:
            await asyncio.sleep(min(1.0, interval - slept))
            slept += 1.0

    async def _kanban_notifier_watcher(self, interval: float = 5.0) -> None:
        """Poll ``kanban_notify_subs`` and deliver terminal events to users.

        Per subscription, claims ``task_events`` newer than the stored cursor
        (kinds in TERMINAL_KINDS), sends one message per event, then advances
        the cursor. The subscription is removed only when the task is
        ``archived``: ``done`` is reversible, so the cursor — not unsubscribing
        — is the dedup mechanism (unsub-on-terminal dropped users when the
        dispatcher respawned a crashed task). All SQLite work runs in a thread;
        one tick's failure never stops the next.
        """
        from gateway.config import Platform as _Platform
        try:
            from hermes_cli import kanban_db as _kb
        except Exception:
            logger.warning("kanban notifier: kanban_db not importable; notifier disabled")
            return

        sub_fail_counts: dict[tuple, int] = getattr(self, "_kanban_sub_fail_counts", {})

        # //// Neoffice — the notifier delivers the worker's terminal result to the user;
        # upstream's 5s poll adds up to ~5s of pure DELIVERY latency to every métier reply
        # (a big chunk of the "20-30s" complaint). Make it config-driven
        # (kanban.notifier_interval_seconds, default 1s) so results reach the voice/desk
        # client promptly. The poll is a light SQLite read in a thread, so 1s is fine.
        try:
            from hermes_cli.config import load_config as _load_config
            _kcfg = (_load_config() or {}).get("kanban", {})
            interval = float(_kcfg.get("notifier_interval_seconds", 1.0) or 1.0)
        except Exception:
            interval = 1.0
        interval = max(interval, 1.0)  # the tick sleeps in whole seconds
        # //// END Neoffice ////
        self._kanban_sub_fail_counts = sub_fail_counts
        notifier_profile = getattr(self, "_kanban_notifier_profile", None) or self._active_profile_name()
        self._kanban_notifier_profile = notifier_profile

        # Initial delay so the gateway can finish wiring adapters.
        await asyncio.sleep(5)

        # Stale done-sub GC: subs survive ``done``, so boards that never
        # archive would accumulate rows scanned every tick. One DELETE per
        # board, at startup (0 → first tick) and at most hourly.
        _gc_next_at = 0.0

        while self._running:
            try:
                _gc_due = time.monotonic() >= _gc_next_at
                _retention = 30
                if _gc_due:
                    _gc_next_at = time.monotonic() + _GC_INTERVAL_SECONDS
                    _retention = _gc_retention_days()

                deliveries = await asyncio.to_thread(
                    _notifier_collect, self, _kb,
                    notifier_profile=notifier_profile, gc_due=_gc_due, gc_retention_days=_retention,
                )
                for d in deliveries:
                    await _KanbanNotification(
                        self, d, platform_cls=_Platform, sub_fail_counts=sub_fail_counts,
                    ).deliver()
            except Exception as exc:
                logger.warning("kanban notifier tick failed: %s", exc)
            await self._sleep_between_ticks(interval)

    def _kanban_sub_op(self, board: Optional[str], op: str, sub: dict, **extra: Any) -> None:
        """Sync helper (runs in to_thread): call ``kanban_db_notify.<op>`` for one subscription on its board."""
        from hermes_cli import kanban_db_connect as _kbc
        from hermes_cli import kanban_db_notify as _kbn
        conn = _kbc.connect(board=board)
        try:
            getattr(_kbn, op)(
                conn, task_id=sub["task_id"], platform=sub["platform"], chat_id=sub["chat_id"],
                thread_id=sub.get("thread_id") or "", **extra,
            )
        finally:
            conn.close()

    def _kanban_advance(self, sub: dict, cursor: int, board: Optional[str] = None) -> None:
        self._kanban_sub_op(board, "advance_notify_cursor", sub, new_cursor=cursor)

    def _kanban_unsub(self, sub: dict, board: Optional[str] = None) -> None:
        self._kanban_sub_op(board, "remove_notify_sub", sub)

    def _kanban_rewind(self, sub: dict, claimed_cursor: int, old_cursor: int, board: Optional[str] = None) -> None:
        """Undo a claimed notification cursor after send failure."""
        self._kanban_sub_op(board, "rewind_notify_cursor", sub, claimed_cursor=claimed_cursor, old_cursor=old_cursor)

    async def _deliver_kanban_artifacts(self, *, adapter, chat_id: str, metadata: dict, event_payload: Optional[dict], task) -> None:
        """Upload artifact files referenced by a completed kanban task.

        Sources, in priority order: ``event_payload['artifacts']``,
        ``event_payload['summary']``, then ``task.result`` (legacy). Paths are
        deduplicated, missing files are skipped (may be mentioned for
        reference only), and upload errors are logged, never raised.
        """
        raw_paths: list[str] = []
        if isinstance(event_payload, dict):
            raw = event_payload.get("artifacts")
            if isinstance(raw, (list, tuple)):
                raw_paths += [item for item in raw if isinstance(item, str)]
            summary = event_payload.get("summary")
            if isinstance(summary, str) and summary:
                raw_paths += adapter.extract_local_files(summary)[0]
        if task is not None and getattr(task, "result", None):
            raw_paths += adapter.extract_local_files(str(task.result))[0]
        candidates: list[str] = []
        for path in raw_paths:
            expanded = os.path.expanduser(path) if path else ""
            if expanded and expanded not in candidates and os.path.isfile(expanded):
                candidates.append(expanded)
        if not candidates:
            return

        from gateway.platforms.base import BasePlatformAdapter
        candidates = BasePlatformAdapter.filter_local_delivery_paths(candidates)
        if not candidates:
            return

        from urllib.parse import quote as _quote

        # Images ride one send_multiple_images call (batch uploads on Signal/Slack).
        image_paths = [p for p in candidates if Path(p).suffix.lower() in _IMAGE_EXTS]
        other_paths = [p for p in candidates if Path(p).suffix.lower() not in _IMAGE_EXTS]
        if image_paths:
            try:
                batch = [(f"file://{_quote(p)}", "") for p in image_paths]
                await adapter.send_multiple_images(chat_id=chat_id, images=batch, metadata=metadata)
            except Exception as exc:
                logger.warning("kanban notifier: image batch upload failed: %s", exc)
        for path in other_paths:
            try:
                if Path(path).suffix.lower() in _VIDEO_EXTS:
                    await adapter.send_video(chat_id=chat_id, video_path=path, metadata=metadata)
                else:
                    await adapter.send_document(chat_id=chat_id, file_path=path, metadata=metadata)
            except Exception as exc:
                logger.warning("kanban notifier: artifact upload (%s) failed: %s", path, exc)

    def _kanban_dispatcher_boot(self) -> Optional[tuple]:
        """Resolve config, kanban_db and the singleton lock; None when the dispatcher must not run.

        Config is read once at boot (restart to apply), except the auto-decompose
        toggle which is re-read every tick. The env var is an escape hatch to
        disable without editing YAML.
        """
        try:
            from hermes_cli.config import load_config as _load_config
        except Exception:
            logger.warning("kanban dispatcher: config loader unavailable; disabled")
            return None
        env_override = os.environ.get("HERMES_KANBAN_DISPATCH_IN_GATEWAY", "").strip().lower()
        if env_override in {"0", "false", "no", "off"}:
            logger.info("kanban dispatcher: disabled via HERMES_KANBAN_DISPATCH_IN_GATEWAY env")
            return None
        try:
            cfg = _load_config()
        except Exception as exc:
            logger.warning("kanban dispatcher: cannot load config (%s); disabled", exc)
            return None
        kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
        if not kanban_cfg.get("dispatch_in_gateway", True):
            logger.info("kanban dispatcher: disabled via config kanban.dispatch_in_gateway=false")
            return None
        try:
            from hermes_cli import kanban_db as _kb
        except Exception:
            logger.warning("kanban dispatcher: kanban_db not importable; dispatcher disabled")
            return None

        # Single-dispatcher backstop (see _acquire_singleton_lock). The lock
        # lives at the machine-global kanban root, so it serialises ALL gateways.
        self._kanban_dispatcher_lock_handle = None
        _lock_path = _kb.kanban_home() / "kanban" / ".dispatcher.lock"
        _lock_handle, _lock_state = _acquire_singleton_lock(_lock_path)
        if _lock_state == "contended":
            logger.info("kanban dispatcher: another gateway already holds the dispatcher "
                        "lock (%s); this gateway will NOT dispatch.", _lock_path)
            return None
        if _lock_state == "held":
            self._kanban_dispatcher_lock_handle = _lock_handle  # hold for process lifetime
            logger.info("kanban dispatcher: holding singleton dispatcher lock (%s)", _lock_path)
        else:
            logger.warning("kanban dispatcher: advisory lock unavailable at %s; proceeding "
                           "on config control alone.", _lock_path)
        return _load_config, _kb, kanban_cfg

    async def _kanban_dispatcher_watcher(self) -> None:
        # //// Neoffice — register the poke event for event-driven dispatch.
        global KANBAN_POKE
        KANBAN_POKE = asyncio.Event()
        # //// END Neoffice ////
        """Embedded kanban dispatcher — one tick every `dispatch_interval_seconds`.

        Gated by `kanban.dispatch_in_gateway` (default True); when false the
        loop exits and an external `hermes kanban daemon` is expected. Each
        tick runs :func:`kanban_db_dispatch.dispatch_once` in a thread; one tick's
        failure never stops the next. Shutdown: ``self._running`` is checked
        between ticks and the in-flight ``to_thread`` returns on its own.
        """
        boot = self._kanban_dispatcher_boot()
        if boot is None:
            return
        _load_config, _kb, kanban_cfg = boot
        settings = _resolve_dispatcher_settings(kanban_cfg, _kb)
        interval = settings.interval

        # Initial delay so adapters are wired before workers spawn (matches the notifier).
        await asyncio.sleep(5)

        # Health telemetry (mirrors `_cmd_daemon`): warn when the ready queue
        # is non-empty but spawns are 0 for N consecutive ticks — usually a
        # broken PATH, missing venv, or credential loss.
        bad_ticks = 0
        last_warn_at = 0
        dispatcher = _KanbanDispatcher(_kb, settings)

        logger.info("kanban dispatcher: embedded in gateway (interval=%.1fs)", interval)
        while self._running:
            try:
                # Reap zombies before per-board work so a board DB failure
                # cannot block cleanup of unrelated workers.
                from hermes_cli import kanban_db_dispatch as _kbd
                pids = await _to_thread_process_service(_kbd.reap_worker_zombies)
                if pids:
                    logger.info("kanban dispatcher: reaped %d zombie worker(s), pids=%s", len(pids), pids)
            except Exception:
                logger.exception("kanban dispatcher: zombie reaper failed")

            try:
                # Emergency stop (`hermes pause`): no auto-decompose or
                # dispatch while paused; running workers finish naturally.
                if not _kanban_dispatch_allowed():
                    bad_ticks = 0
                else:
                    # Re-read the auto-decompose toggle live so disabling it
                    # takes effect on the next tick, not on restart.
                    _ad_enabled, _ad_per_tick = _resolve_auto_decompose_settings(_load_config)
                    # See #49638.
                    if _ad_enabled:
                        await _to_thread_process_service(dispatcher.auto_decompose_tick, _ad_per_tick)
                    results = await _to_thread_process_service(dispatcher.tick_once)
                    any_spawned = _log_spawn_results(results)
                    ready_pending = await _to_thread_process_service(dispatcher.ready_nonempty)
                    bad_ticks = bad_ticks + 1 if ready_pending and not any_spawned else 0
                now = int(time.time())
                if bad_ticks >= _HEALTH_WINDOW and now - last_warn_at >= 300:
                    logger.warning(
                        "kanban dispatcher stuck: ready queue non-empty for "
                        "%d consecutive ticks but 0 workers spawned. Check "
                        "profile health (venv, PATH, credentials) and "
                        "`hermes kanban list --status ready`.",
                        bad_ticks,
                    )
                    last_warn_at = now
            except asyncio.CancelledError:
                logger.debug("kanban dispatcher: cancelled")
                self._release_kanban_dispatcher_lock()
                raise
            except Exception:
                logger.exception("kanban dispatcher: unexpected watcher error")

            # Sleep in 1s slices so shutdown is snappy — otherwise a stop()
            # waits up to `interval` seconds for the current sleep to finish.
            # //// Neoffice — a poke (task created by the chat router) breaks
            # the wait immediately: dispatch happens the instant work exists
            # instead of at the next periodic tick.
            slept = 0.0
            while slept < interval and self._running:
                ev = KANBAN_POKE
                if ev is not None:
                    try:
                        await asyncio.wait_for(ev.wait(), timeout=min(1.0, interval - slept))
                        ev.clear()
                        break  # poked → tick now
                    except asyncio.TimeoutError:
                        pass
                else:
                    await asyncio.sleep(min(1.0, interval - slept))
                slept += 1.0
            # //// END Neoffice ////
            await self._sleep_between_ticks(interval)

        self._release_kanban_dispatcher_lock()


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# Names external plugins imported from this module before the Sep 2026 decomposition.
# Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
# The whole block is removed by reverting the commit that added it.
from typing import Callable  # noqa: F401,E402
from contextvars import Context  # noqa: F401,E402
import logging  # noqa: F401,E402
import re  # noqa: F401,E402
import sqlite3  # noqa: F401,E402


_PLUGIN_COMPAT_LAZY = {
    't': ('agent.i18n', 't'),
}


def __getattr__(name):  # PEP 562 — lazy so no import cycles
    target = _PLUGIN_COMPAT_LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib
    from hermes_cli.plugin_compat import warn_once
    warn_once(__name__, name, *target)
    return getattr(importlib.import_module(target[0]), target[1])
# ---- END PLUGIN-COMPAT ----
