"""Health checker — periodic readiness probes.

Each "check" is a callable that returns ``(ok: bool, detail: str)``.
The checker runs every check on a schedule and stores the rolling
result so ``/health`` and ``/ready`` endpoints can answer instantly
without re-running them per request.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from typing import Callable, Optional

from app.core.logging import get_logger
from app.monitoring.monitoring_config import get_config
from app.monitoring.monitoring_events import emit

logger = get_logger(__name__)


# A check is a function returning (ok, detail).
CheckFn = Callable[[], tuple[bool, str]]


# ── Default checks — light-weight, no I/O over the network ──────


def _check_lifecycle() -> tuple[bool, str]:
    try:
        from app.distributed_system.node_lifecycle import (
            NodeState, get_lifecycle,
        )
        s = get_lifecycle().state()
        return (s in (NodeState.READY, NodeState.STARTING),
                f"lifecycle={s.value}")
    except Exception as e:
        return False, f"lifecycle_unavailable:{e}"


def _check_partition() -> tuple[bool, str]:
    try:
        from app.distributed_system.partition_detector import is_majority
        return is_majority(), "majority" if is_majority() else "minority"
    except Exception as e:
        return False, f"partition_unavailable:{e}"


def _check_backpressure() -> tuple[bool, str]:
    try:
        from app.services.backpressure import get_backpressure
        snap = get_backpressure().snapshot()
        level = snap.get("level", "normal")
        return level != "rejected", f"level={level}"
    except Exception as e:
        return True, f"backpressure_unavailable:{e}"


def _check_audit_chain() -> tuple[bool, str]:
    try:
        from app.services.audit_replication import get_audit_replicator
        v = get_audit_replicator().verify_chain(max_entries=100)
        return v.get("ok", True), f"verified={v.get('entries', 0)}"
    except Exception as e:
        return True, f"audit_unavailable:{e}"


_DEFAULT_CHECKS: dict[str, CheckFn] = {
    "lifecycle":     _check_lifecycle,
    "partition":     _check_partition,
    "backpressure":  _check_backpressure,
    "audit_chain":   _check_audit_chain,
}


# ── Health checker singleton ────────────────────────────────────


class HealthChecker:
    _singleton: "HealthChecker | None" = None

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._checks: dict[str, CheckFn] = dict(_DEFAULT_CHECKS)
        self._latest: dict[str, dict] = {}
        self._history: deque = deque()
        self._loop_task: Optional[asyncio.Task] = None
        self._running = False
        self._cap = get_config().health_history_max

    @classmethod
    def instance(cls) -> "HealthChecker":
        if cls._singleton is None:
            cls._singleton = HealthChecker()
        return cls._singleton

    # ── Mutators ────────────────────────────────────────────

    def register(self, name: str, fn: CheckFn) -> None:
        with self._lock:
            self._checks[name] = fn

    def unregister(self, name: str) -> None:
        with self._lock:
            self._checks.pop(name, None)

    # ── Run ─────────────────────────────────────────────────

    def run_all(self) -> dict:
        results = {}
        with self._lock:
            checks = dict(self._checks)
        for name, fn in checks.items():
            try:
                ok, detail = fn()
            except Exception as e:
                ok, detail = False, f"raised:{e}"
            results[name] = {"ok": bool(ok), "detail": detail}

        ok_count = sum(1 for r in results.values() if r["ok"])
        all_ok = ok_count == len(results)
        snapshot = {
            "ok":           all_ok,
            "ok_count":     ok_count,
            "total_checks": len(results),
            "checks":       results,
            "ts":           time.time(),
        }
        with self._lock:
            self._latest = snapshot
            self._history.append(snapshot)
            while len(self._history) > self._cap:
                self._history.popleft()
        emit("health.checked", {"ok": all_ok, "ok_count": ok_count})
        if not all_ok:
            failing = [n for n, r in results.items() if not r["ok"]]
            emit("health.failing", {"failing": failing})
        return snapshot

    def latest(self) -> dict:
        with self._lock:
            return dict(self._latest) or {"ok": None, "checks": {}}

    def history(self, limit: int = 20) -> list[dict]:
        with self._lock:
            return list(self._history)[-int(limit):]

    # ── Background loop ─────────────────────────────────────

    async def _run_loop(self) -> None:
        cfg = get_config()
        self._running = True
        logger.info("monitoring_health_started",
                    interval_sec=cfg.health_check_interval_sec)
        try:
            while self._running:
                try:
                    self.run_all()
                except Exception as e:
                    logger.warning("monitoring_health_failed", error=str(e))
                await asyncio.sleep(cfg.health_check_interval_sec)
        finally:
            logger.info("monitoring_health_stopped")

    def start(self) -> None:
        if self._loop_task is not None and not self._loop_task.done():
            return
        try:
            loop = asyncio.get_event_loop()
            self._loop_task = loop.create_task(
                self._run_loop(), name="monitoring-health",
            )
        except RuntimeError:
            logger.warning("monitoring_health_no_event_loop_yet")

    def stop(self) -> None:
        self._running = False
        if self._loop_task is not None:
            self._loop_task.cancel()
            self._loop_task = None


def get_health_checker() -> HealthChecker:
    return HealthChecker.instance()
