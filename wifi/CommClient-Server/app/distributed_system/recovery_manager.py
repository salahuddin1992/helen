"""Recovery manager — orchestrates self-healing.

Listens to events on the distributed bus and runs the right
remediation:

  * ``partition.detected``    → trigger gossip + state-sync.
  * ``member.evicted``        → drop their phi detector entry.
  * ``consensus.failed``      → enqueue a delayed retry.

Pure orchestration — the heavy work happens in the underlying
services.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from app.core.logging import get_logger
from app.distributed_system import (
    failure_detector, gossip_manager, state_sync,
)
from app.distributed_system.distributed_config import get_config
from app.distributed_system.distributed_events import emit, subscribe

logger = get_logger(__name__)


class RecoveryManager:
    _singleton: "RecoveryManager | None" = None

    def __init__(self) -> None:
        self._loop_task: Optional[asyncio.Task] = None
        self._running = False
        self._actions_taken = 0
        self._last_action_at: float = 0.0
        self._subscribed = False

    @classmethod
    def instance(cls) -> "RecoveryManager":
        if cls._singleton is None:
            cls._singleton = RecoveryManager()
        return cls._singleton

    # ── Event handlers ─────────────────────────────────────

    def _spawn(self, coro) -> None:
        """Hold a strong reference to fire-and-forget tasks so the
        asyncio GC doesn't kill them mid-flight (the docs explicitly
        warn that ``loop.create_task`` results must be retained)."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                # No loop available — best-effort, drop the work.
                return
        task = loop.create_task(coro)
        if not hasattr(self, "_bg_tasks"):
            self._bg_tasks: set[asyncio.Task] = set()
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    def _on_partition(self, name: str, payload: dict) -> None:
        cfg = get_config()
        if not cfg.enable_auto_recovery:
            return
        # Trigger an aggressive gossip pass + state sync.
        self._spawn(gossip_manager.trigger_now())
        self._spawn(state_sync.sync_now())
        self._record_action("partition_recovery_started")

    def _on_member_evicted(self, name: str, payload: dict) -> None:
        sid = payload.get("node_id")
        if not sid:
            return
        failure_detector.evict(sid)
        self._record_action(f"evicted_{sid[:12]}")

    def _on_consensus_failed(self, name: str, payload: dict) -> None:
        # Schedule a delayed retry via state sync — most consensus
        # failures heal once the network is converged.
        self._spawn(state_sync.sync_now())
        self._record_action("consensus_retry_state_sync")

    def _record_action(self, label: str) -> None:
        import time
        self._actions_taken += 1
        self._last_action_at = time.time()
        emit("recovery.action", {"label": label})

    # ── Subscription wiring ────────────────────────────────

    def _subscribe(self) -> None:
        if self._subscribed:
            return
        subscribe("partition.detected", self._on_partition)
        subscribe("member.evicted",     self._on_member_evicted)
        subscribe("consensus.failed",   self._on_consensus_failed)
        self._subscribed = True

    # ── Background watchdog ────────────────────────────────

    async def _run_loop(self) -> None:
        cfg = get_config()
        self._running = True
        self._subscribe()
        logger.info("ds_recovery_started", interval_sec=cfg.recovery_check_sec)
        try:
            while self._running:
                # Periodic safety net: if we're in minority, kick off
                # one gossip cycle even without an explicit event.
                try:
                    from app.distributed_system import partition_detector as pd
                    if not pd.is_majority():
                        await gossip_manager.trigger_now()
                        self._record_action("watchdog_minority")
                except Exception as e:
                    logger.debug("ds_recovery_watchdog_err", error=str(e))
                await asyncio.sleep(cfg.recovery_check_sec)
        finally:
            logger.info("ds_recovery_stopped")

    def start(self) -> None:
        if self._loop_task is not None and not self._loop_task.done():
            return
        try:
            loop = asyncio.get_event_loop()
            self._loop_task = loop.create_task(
                self._run_loop(), name="ds-recovery",
            )
        except RuntimeError:
            self._subscribe()
            logger.warning("ds_recovery_no_event_loop_yet")

    def stop(self) -> None:
        self._running = False
        if self._loop_task is not None:
            self._loop_task.cancel()
            self._loop_task = None

    def stats(self) -> dict:
        return {
            "actions_taken":   self._actions_taken,
            "last_action_at":  self._last_action_at,
            "subscribed":      self._subscribed,
        }


def get_recovery_manager() -> RecoveryManager:
    return RecoveryManager.instance()
