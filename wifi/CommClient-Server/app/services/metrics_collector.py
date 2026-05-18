"""
Phase 2 / Module F — Time-series metrics collector.

A singleton background sampler that snapshots host + application metrics
every ``RESOLUTION_SEC`` seconds and keeps a ``HORIZON_SEC`` ring buffer
in memory. The admin metrics router (``admin_metrics.py``) calls into this
module for both point-in-time snapshots and historical series.

Why in-memory?
--------------
The whole purpose of this view is "what is the box doing **right now** and
over the last hour". Pushing through Prometheus + an external TSDB would be
overkill on a single-box LAN deployment. If a Prometheus client gauge
exists, we mirror our values into it so the existing /metrics endpoint
stays consistent.

Metrics
-------
Host (via psutil):
    cpu_percent          — system-wide CPU %
    memory_percent       — system memory %
    memory_mb            — RSS of *this* process (MB)
    disk_io_read         — bytes/s read across all disks
    disk_io_write        — bytes/s written across all disks
    network_recv         — bytes/s received
    network_sent         — bytes/s sent

Application (via call-out probes):
    active_calls         — current rooms in active_call_service
    connected_clients    — Socket.IO connected sockets
    jwt_issued_per_min   — sliding 60-s counter
    db_connections       — async engine pool size+overflow
    queue_depth          — DLQ / outbox depth summed
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

try:
    import psutil  # type: ignore
except ImportError:                                                 # pragma: no cover
    psutil = None                                                   # type: ignore

logger = logging.getLogger(__name__)


# ── Configuration ──────────────────────────────────────────
RESOLUTION_SEC = 5
HORIZON_SEC = 3600  # one hour
SAMPLES_KEPT = HORIZON_SEC // RESOLUTION_SEC   # 720 points/metric


METRIC_NAMES: tuple[str, ...] = (
    "cpu_percent",
    "memory_percent",
    "memory_mb",
    "disk_io_read",
    "disk_io_write",
    "network_recv",
    "network_sent",
    "active_calls",
    "connected_clients",
    "jwt_issued_per_min",
    "db_connections",
    "queue_depth",
)


@dataclass
class _Sample:
    ts: float
    value: float


@dataclass
class _Series:
    name: str
    points: deque[_Sample] = field(
        default_factory=lambda: deque(maxlen=SAMPLES_KEPT)
    )

    def append(self, ts: float, value: float) -> None:
        self.points.append(_Sample(ts=ts, value=float(value)))

    def since(self, t0: float) -> list[dict[str, float]]:
        return [{"ts": p.ts, "v": p.value} for p in self.points if p.ts >= t0]

    def all(self) -> list[dict[str, float]]:
        return [{"ts": p.ts, "v": p.value} for p in self.points]

    def latest(self) -> Optional[float]:
        if not self.points:
            return None
        return self.points[-1].value


class MetricsCollector:
    """Process-wide singleton."""

    _instance: Optional["MetricsCollector"] = None
    _instance_lock = threading.Lock()

    @classmethod
    def get(cls) -> "MetricsCollector":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def __init__(self) -> None:
        self._series: dict[str, _Series] = {
            name: _Series(name=name) for name in METRIC_NAMES
        }
        self._lock = asyncio.Lock()
        self._task: Optional[asyncio.Task] = None
        self._stop: Optional[asyncio.Event] = None
        self._started_at: Optional[float] = None
        self._proc = psutil.Process(os.getpid()) if psutil else None
        # Counter probes — set externally via probes()
        self._jwt_window: deque[float] = deque(maxlen=2048)
        self._probes: dict[str, Callable[[], Any]] = {}
        # Cache for rate calculations
        self._last_disk: Optional[tuple[float, int, int]] = None
        self._last_net: Optional[tuple[float, int, int]] = None

    # ── External probe registration ────────────────────────

    def register_probe(self, name: str, fn: Callable[[], Any]) -> None:
        """Set a synchronous (cheap) callback that returns a numeric value
        for one of the application metric names. Coroutines are allowed —
        we'll await them inside the sampler."""
        if name not in METRIC_NAMES:
            raise ValueError(f"unknown metric: {name}")
        self._probes[name] = fn

    def record_jwt_issued(self) -> None:
        """Increment the JWT-per-minute sliding counter. Cheap, lock-free."""
        self._jwt_window.append(time.time())

    # ── Lifecycle ──────────────────────────────────────────

    def start(self) -> None:
        """Kick off the background sampler. Idempotent."""
        if self._task is not None and not self._task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:                       # called outside event loop
            return
        self._stop = asyncio.Event()
        self._started_at = time.time()
        self._task = loop.create_task(self._run(), name="metrics-collector")

    async def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
        self._task = None

    @property
    def started_at(self) -> Optional[float]:
        return self._started_at

    # ── Sampler loop ───────────────────────────────────────

    async def _run(self) -> None:
        assert self._stop is not None
        # Prime psutil cpu_percent — first call returns 0.
        if psutil:
            try:
                psutil.cpu_percent(interval=None)
            except Exception:
                pass
        while not self._stop.is_set():
            try:
                await self._sample_once()
            except Exception as e:
                logger.warning("metrics_sample_failed: %s", e)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=RESOLUTION_SEC)
            except asyncio.TimeoutError:
                pass

    async def _sample_once(self) -> None:
        now = time.time()
        values: dict[str, float] = {}

        if psutil and self._proc is not None:
            try:
                values["cpu_percent"] = float(psutil.cpu_percent(interval=None))
                values["memory_percent"] = float(psutil.virtual_memory().percent)
                values["memory_mb"] = self._proc.memory_info().rss / (1024 * 1024)
            except Exception:
                pass
            try:
                io = psutil.disk_io_counters()
                if io is not None:
                    if self._last_disk is None:
                        values["disk_io_read"] = 0.0
                        values["disk_io_write"] = 0.0
                    else:
                        dt = max(1e-3, now - self._last_disk[0])
                        values["disk_io_read"] = max(0.0, (io.read_bytes - self._last_disk[1]) / dt)
                        values["disk_io_write"] = max(0.0, (io.write_bytes - self._last_disk[2]) / dt)
                    self._last_disk = (now, io.read_bytes, io.write_bytes)
            except Exception:
                pass
            try:
                net = psutil.net_io_counters()
                if net is not None:
                    if self._last_net is None:
                        values["network_recv"] = 0.0
                        values["network_sent"] = 0.0
                    else:
                        dt = max(1e-3, now - self._last_net[0])
                        values["network_recv"] = max(0.0, (net.bytes_recv - self._last_net[1]) / dt)
                        values["network_sent"] = max(0.0, (net.bytes_sent - self._last_net[2]) / dt)
                    self._last_net = (now, net.bytes_recv, net.bytes_sent)
            except Exception:
                pass

        # Application probes
        for name, fn in self._probes.items():
            try:
                v = fn()
                if asyncio.iscoroutine(v):
                    v = await v
                values[name] = float(v or 0)
            except Exception as e:
                logger.debug("metrics_probe_%s_failed: %s", name, e)

        # JWT rate from sliding window
        cutoff = now - 60.0
        while self._jwt_window and self._jwt_window[0] < cutoff:
            self._jwt_window.popleft()
        values.setdefault("jwt_issued_per_min", float(len(self._jwt_window)))

        async with self._lock:
            for name in METRIC_NAMES:
                series = self._series[name]
                series.append(now, values.get(name, series.latest() or 0.0))

    # ── Read API ───────────────────────────────────────────

    def snapshot(self) -> dict[str, Any]:
        """Latest value for every series, plus boot/uptime."""
        now = time.time()
        out: dict[str, Any] = {
            "ts": now,
            "uptime_sec": (now - self._started_at) if self._started_at else 0,
            "resolution_sec": RESOLUTION_SEC,
            "horizon_sec": HORIZON_SEC,
            "metrics": {n: self._series[n].latest() for n in METRIC_NAMES},
        }
        return out

    def series(self, metric: str, since_sec: float = HORIZON_SEC) -> dict[str, Any]:
        if metric not in METRIC_NAMES:
            raise KeyError(metric)
        cutoff = time.time() - since_sec
        return {
            "metric": metric,
            "since": cutoff,
            "points": self._series[metric].since(cutoff),
        }

    def all_series(self, since_sec: float = HORIZON_SEC) -> dict[str, Any]:
        cutoff = time.time() - since_sec
        return {
            "since": cutoff,
            "resolution_sec": RESOLUTION_SEC,
            "metrics": {
                n: self._series[n].since(cutoff) for n in METRIC_NAMES
            },
        }


# Convenience module-level accessor
metrics_collector = MetricsCollector.get()
