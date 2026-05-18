"""
Load monitor — periodic snapshot of this server's pressure metrics
and broadcaster of those metrics to peers via the registry service.

Why
---
Routing and admission decisions need fresh load data. Without it:
  * route_planner happily routes through an overloaded server
  * SFU orchestrator allocates against a saturated SFU
  * backpressure_service has no signal to throttle

This module collects metrics every 5s and publishes them to the
``ServerRegistryService``. ``BackpressureService`` and ``RoutePlanner``
read from the same registry to make decisions.

Metrics collected
-----------------
  * cpu_percent (psutil sample)
  * memory_percent (psutil)
  * event_loop_lag_ms (drift of an asyncio sleep — process-internal)
  * active_sockets (socket.io connection count)
  * active_calls (call_service)
  * queue_depth_p0..p4 (event_priority_queue)
  * sfu_pressure_pct (sfu_orchestrator if available, else 0)

Health score
------------
A scalar 0.0–1.0 derived from the metrics. ``route_planner`` uses it
as an edge-weight multiplier. Defined explicitly so future changes
to weighting are observable in one place.
"""

from __future__ import annotations

import asyncio
import time
from typing import Optional

from app.core.logging import get_logger
from app.services.server_registry_service import LoadSnapshot

logger = get_logger(__name__)

SAMPLE_INTERVAL_SEC = 5.0
EVENT_LOOP_LAG_PROBE_SEC = 0.1  # asyncio.sleep target; measured drift is the lag


class LoadMonitor:
    def __init__(
        self,
        *,
        this_server_id: str,
        registry_service,
        priority_router=None,
        socket_count_provider=None,
        active_calls_provider=None,
        sfu_pressure_provider=None,
    ) -> None:
        self._sid = this_server_id
        self._registry = registry_service
        self._router = priority_router
        self._sockets = socket_count_provider or (lambda: 0)
        self._calls = active_calls_provider or (lambda: 0)
        self._sfu_pressure = sfu_pressure_provider or (lambda: 0.0)
        self._task: Optional[asyncio.Task] = None
        self._stopped = asyncio.Event()
        self._last_snapshot: Optional[LoadSnapshot] = None

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._sample_loop())

    async def stop(self) -> None:
        self._stopped.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, BaseException):
                pass
            self._task = None

    async def snapshot(self) -> LoadSnapshot:
        return await self._take_snapshot()

    @property
    def last(self) -> Optional[LoadSnapshot]:
        return self._last_snapshot

    # ── Internal ───────────────────────────────────────────────

    async def _sample_loop(self) -> None:
        try:
            while not self._stopped.is_set():
                try:
                    snap = await self._take_snapshot()
                    self._last_snapshot = snap
                    await self._registry.publish_load(snap)
                except Exception as e:
                    logger.warning("load_monitor_iteration_failed", error=str(e))
                try:
                    await asyncio.wait_for(
                        self._stopped.wait(),
                        timeout=SAMPLE_INTERVAL_SEC,
                    )
                    return
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            return

    async def _take_snapshot(self) -> LoadSnapshot:
        cpu = await self._cpu_percent()
        mem = await self._memory_percent()
        loop_lag = await self._event_loop_lag_ms()
        sockets = self._safe_int(self._sockets)
        calls = self._safe_int(self._calls)
        sfu_pct = self._safe_float(self._sfu_pressure)

        depths = self._router.all_depths() if self._router is not None else {}
        snap = LoadSnapshot(
            server_id=self._sid,
            timestamp=time.time(),
            cpu_percent=cpu,
            memory_percent=mem,
            event_loop_lag_ms=loop_lag,
            active_sockets=sockets,
            active_calls=calls,
            queue_depth_p0=depths.get("P0", 0),
            queue_depth_p1=depths.get("P1", 0),
            health_score=self._derive_health(cpu, mem, loop_lag, depths, sfu_pct),
        )
        return snap

    @staticmethod
    def _derive_health(
        cpu: float, mem: float, lag_ms: float,
        depths: dict, sfu_pct: float,
    ) -> float:
        # Each component contributes an "unhealthiness" delta. We
        # subtract from 1.0. Anything above 1.0 unhealthiness floors
        # at 0.0.
        u = 0.0
        u += max(0.0, (cpu - 50) / 50)        # 0 below 50%, 1 at 100%
        u += max(0.0, (mem - 50) / 50)
        u += max(0.0, (lag_ms - 50) / 200)    # 0 below 50ms, 1 at 250ms
        u += min(1.0, depths.get("P0", 0) / 500)
        u += min(1.0, depths.get("P1", 0) / 1000)
        u += sfu_pct / 100.0
        # Average across 6 components.
        unhealthy = min(1.0, u / 6)
        return max(0.0, 1.0 - unhealthy)

    @staticmethod
    def _safe_int(provider) -> int:
        try:
            v = provider()
            return int(v) if v is not None else 0
        except Exception:
            return 0

    @staticmethod
    def _safe_float(provider) -> float:
        try:
            v = provider()
            return float(v) if v is not None else 0.0
        except Exception:
            return 0.0

    @staticmethod
    async def _cpu_percent() -> float:
        # psutil is in requirements.txt for production deployments.
        # We import lazily so test environments without psutil still
        # boot and return 0.0.
        try:
            import psutil  # type: ignore
            # Non-blocking sample; first call returns 0.0 then is
            # accurate. We accept the imprecision.
            return psutil.cpu_percent(interval=None)
        except Exception:
            return 0.0

    @staticmethod
    async def _memory_percent() -> float:
        try:
            import psutil  # type: ignore
            return psutil.virtual_memory().percent
        except Exception:
            return 0.0

    @staticmethod
    async def _event_loop_lag_ms() -> float:
        # Schedule an asyncio.sleep for a known target. Difference
        # between target and actual elapsed = approximate loop lag.
        target = EVENT_LOOP_LAG_PROBE_SEC
        t0 = time.perf_counter()
        await asyncio.sleep(target)
        elapsed = time.perf_counter() - t0
        lag = max(0.0, (elapsed - target) * 1000.0)
        return lag


# ── Module-level singleton ──────────────────────────────────────────

_svc: Optional[LoadMonitor] = None


def get_load_monitor() -> Optional[LoadMonitor]:
    return _svc


def configure(**kwargs) -> LoadMonitor:
    global _svc
    _svc = LoadMonitor(**kwargs)
    return _svc
