"""Recovery manager — listens for failure events + triggers healing.

Subscribes to:

  * ``breaker.open``        → drop peer from phi accrual + try gossip
  * ``probe.failing``       → emit alert + log
  * ``retry.exhausted``     → audit log entry
  * ``partition.detected``  → trigger gossip + state-sync (re-uses
                              services.state_reconciliation)

Pure orchestration. The heavy lifting happens in the underlying
services — this module just wires events to the right call.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Optional

from app.core.logging import get_logger
from app.resilience.resilience_config import get_config
from app.resilience.resilience_events import emit, subscribe

logger = get_logger(__name__)


class RecoveryManager:
    _singleton: "RecoveryManager | None" = None

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._loop_task: Optional[asyncio.Task] = None
        self._running = False
        self._actions = 0
        self._subscribed = False

    @classmethod
    def instance(cls) -> "RecoveryManager":
        if cls._singleton is None:
            cls._singleton = RecoveryManager()
        return cls._singleton

    # ── Event handlers ────────────────────────────────────

    def _on_breaker_open(self, name: str, payload: dict) -> None:
        cfg = get_config()
        if not cfg.enable_auto_recovery:
            return
        target = payload.get("target") or ""
        if not target:
            return
        try:
            from app.services.phi_accrual import get_phi_registry
            get_phi_registry().evict(target)
        except Exception:
            pass
        self._record(f"breaker_open:{target[:16]}")

    def _on_probe_failing(self, name: str, payload: dict) -> None:
        emit("recovery.alert", {
            "source": "probe",
            "name":   payload.get("name"),
            "detail": payload.get("detail"),
        })
        self._record(f"probe_alert:{payload.get('name')}")

    def _on_retry_exhausted(self, name: str, payload: dict) -> None:
        try:
            from app.services.audit_replication import get_audit_replicator
            get_audit_replicator().append_local(
                event="retry.exhausted",
                actor="resilience",
                payload=payload,
            )
        except Exception:
            pass
        self._record(f"retry_exhausted:{payload.get('task_kind')}")

    def _on_partition_detected(self, name: str, payload: dict) -> None:
        cfg = get_config()
        if not cfg.enable_auto_recovery:
            return
        try:
            loop = asyncio.get_event_loop()
            from app.services.state_reconciliation import _reconcile_once
            from app.services.anti_entropy import _cycle as ae_cycle
            loop.create_task(_reconcile_once())
            loop.create_task(ae_cycle())
        except RuntimeError:
            pass
        self._record("partition_recovery")

    def _record(self, label: str) -> None:
        with self._lock:
            self._actions += 1
        emit("recovery.action", {"label": label})

    # ── Wire-up ───────────────────────────────────────────

    def _ensure_subscribed(self) -> None:
        with self._lock:
            if self._subscribed:
                return
            self._subscribed = True
        subscribe("breaker.open",       self._on_breaker_open)
        subscribe("probe.failing",      self._on_probe_failing)
        subscribe("retry.exhausted",    self._on_retry_exhausted)
        subscribe("partition.detected", self._on_partition_detected)

    # ── Background watchdog ───────────────────────────────

    async def _run_loop(self) -> None:
        cfg = get_config()
        self._running = True
        self._ensure_subscribed()
        logger.info("res_recovery_started",
                    interval_sec=cfg.recovery_check_sec)
        try:
            while self._running:
                # Periodic heartbeat so monitoring can detect a wedged
                # recovery manager (event-driven only — silence here
                # could mean either "nothing failing" or "watchdog
                # itself died"). Emit our running stats so dashboards
                # can graph action rate over time.
                try:
                    with self._lock:
                        actions = self._actions
                    emit("recovery.heartbeat", {
                        "actions_taken": actions,
                        "subscribed": self._subscribed,
                    })
                except Exception:
                    pass
                await asyncio.sleep(cfg.recovery_check_sec)
        finally:
            logger.info("res_recovery_stopped")

    def start(self) -> None:
        if self._loop_task is not None and not self._loop_task.done():
            return
        try:
            loop = asyncio.get_event_loop()
            self._loop_task = loop.create_task(
                self._run_loop(), name="resilience-recovery",
            )
        except RuntimeError:
            self._ensure_subscribed()
            logger.warning("res_recovery_no_event_loop_yet")

    def stop(self) -> None:
        self._running = False
        if self._loop_task is not None:
            self._loop_task.cancel()
            self._loop_task = None

    def stats(self) -> dict:
        with self._lock:
            return {"actions_taken": self._actions,
                    "subscribed":    self._subscribed}


def get_recovery_manager() -> RecoveryManager:
    return RecoveryManager.instance()
