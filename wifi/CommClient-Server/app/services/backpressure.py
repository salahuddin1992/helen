"""
Cooperative backpressure — fail fast when overloaded.

Under sustained high load a server hits a knee where adding more work
makes everything slower (queues fill, GC pressure rises, GC pauses
cascade into timeouts, retries flood, ...). The standard cure is
backpressure: signal upstream "I'm full, slow down" before total
collapse.

This module exposes a single ``BackpressureGate`` singleton that:

  * Watches CPU%, memory%, queue depth, and active sockets every tick.
  * Computes a ``saturation`` score in [0.0, 1.0].
  * Below ``SOFT_THRESHOLD`` (0.7): normal — accept everything.
  * Between SOFT and HARD (0.7-0.9): degraded — return 503 to
    non-essential requests, tag responses with ``X-Backpressure: warn``.
  * Above ``HARD_THRESHOLD`` (0.9): rejected — return 503 to almost
    every request except admin/healthchecks, tag with
    ``X-Backpressure: hard``.

Peer-aware
----------
The state is exposed via ``/api/cluster/backpressure`` so peers in
the relay chain can see we're saturated and route around us instead
of piling more requests onto an already-overloaded box.

The gate is a *cooperative* circuit, not enforced — caller decides
what to do with ``decide()`` output. The default FastAPI middleware
in ``app.api.middleware.backpressure`` honours it for hot-path
endpoints (chat, calls, file upload).
"""

from __future__ import annotations

import asyncio
import threading
import time
from enum import Enum
from typing import Optional

from app.core.logging import get_logger

logger = get_logger(__name__)


SOFT_THRESHOLD = 0.70
HARD_THRESHOLD = 0.90
CHECK_INTERVAL_SEC = 5.0


class BackpressureLevel(str, Enum):
    NORMAL   = "normal"
    DEGRADED = "degraded"
    REJECTED = "rejected"


# ── Saturation scoring ──────────────────────────────────────────


def _saturation_inputs() -> dict:
    """Pull live load metrics — falls back to defaults if any
    component isn't reachable."""
    cpu_pct = 0.0
    rss_pct = 0.0
    queue_pct = 0.0
    socket_pct = 0.0
    try:
        from app.services.control_plane import ControlPlane
        s = ControlPlane.instance().status()
        inp = s.get("inputs") or {}
        cpu_pct = float(inp.get("cpu_p95") or 0.0)
        rss_pct = float(inp.get("rss_p95") or 0.0)
        socket_pct = (
            100.0 * float(inp.get("active_sockets") or 0)
            / max(1, float(inp.get("max_sockets") or 1))
        )
    except Exception:
        pass
    try:
        from app.services.dead_letter_service import dead_letter_service
        dl = dead_letter_service.queue_size()
        queue_pct = min(100.0, dl / 100.0)  # 100 dead letters = 100% pressure
    except Exception:
        pass
    return {
        "cpu_pct":    cpu_pct,
        "rss_pct":    rss_pct,
        "socket_pct": socket_pct,
        "queue_pct":  queue_pct,
    }


def _score(inputs: dict) -> float:
    """Weighted blend → 0..1.

    CPU dominates because every other dimension feeds back into it.
    Sockets matter for connection-bound workloads. Queue depth
    catches "we accepted too much, can't drain it".
    """
    cpu  = min(1.0, inputs["cpu_pct"]    / 100.0)
    rss  = min(1.0, inputs["rss_pct"]    / 100.0)
    sock = min(1.0, inputs["socket_pct"] / 100.0)
    q    = min(1.0, inputs["queue_pct"]  / 100.0)
    return round(
        0.45 * cpu + 0.20 * rss + 0.20 * sock + 0.15 * q,
        4,
    )


# ── Gate singleton ──────────────────────────────────────────────


class BackpressureGate:
    _singleton: "BackpressureGate | None" = None

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._level: BackpressureLevel = BackpressureLevel.NORMAL
        self._saturation: float = 0.0
        self._inputs: dict = {}
        self._last_change_at: float = 0.0
        self._last_tick_at: float = 0.0

    @classmethod
    def instance(cls) -> "BackpressureGate":
        if cls._singleton is None:
            cls._singleton = BackpressureGate()
        return cls._singleton

    def tick(self) -> None:
        inputs = _saturation_inputs()
        s = _score(inputs)
        with self._lock:
            old = self._level
            if s >= HARD_THRESHOLD:
                self._level = BackpressureLevel.REJECTED
            elif s >= SOFT_THRESHOLD:
                self._level = BackpressureLevel.DEGRADED
            else:
                self._level = BackpressureLevel.NORMAL
            self._saturation = s
            self._inputs = inputs
            self._last_tick_at = time.time()
            if self._level != old:
                self._last_change_at = self._last_tick_at
                logger.info(
                    "backpressure_level_changed",
                    old=old.value,
                    new=self._level.value,
                    saturation=s,
                    inputs=inputs,
                )

    def decide(self, *, essential: bool = False) -> tuple[bool, BackpressureLevel]:
        """Return (accept, level). When essential=True (admin /
        healthcheck) we always accept until the OS itself is dying."""
        with self._lock:
            level = self._level
        if essential:
            return True, level
        if level == BackpressureLevel.NORMAL:
            return True, level
        if level == BackpressureLevel.DEGRADED:
            return True, level  # caller may shed sub-priorities
        # REJECTED → fail fast.
        return False, level

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "level":         self._level.value,
                "saturation":    self._saturation,
                "inputs":        dict(self._inputs),
                "last_tick_at":  self._last_tick_at,
                "last_change_at": self._last_change_at,
                "thresholds":    {
                    "soft": SOFT_THRESHOLD,
                    "hard": HARD_THRESHOLD,
                },
            }


def get_backpressure() -> BackpressureGate:
    return BackpressureGate.instance()


# ── Background loop ─────────────────────────────────────────────


_loop_task: Optional[asyncio.Task] = None
_running = False


async def _bp_loop() -> None:
    global _running
    _running = True
    logger.info("backpressure_loop_started", interval_sec=CHECK_INTERVAL_SEC)
    try:
        while _running:
            try:
                get_backpressure().tick()
            except Exception as e:
                logger.warning("backpressure_tick_failed", error=str(e))
            await asyncio.sleep(CHECK_INTERVAL_SEC)
    finally:
        logger.info("backpressure_loop_stopped")


def start_backpressure_loop() -> None:
    global _loop_task
    if _loop_task is not None and not _loop_task.done():
        return
    try:
        loop = asyncio.get_event_loop()
        _loop_task = loop.create_task(_bp_loop(), name="backpressure")
    except RuntimeError:
        logger.warning("backpressure_no_event_loop_yet")


def stop_backpressure_loop() -> None:
    global _running, _loop_task
    _running = False
    if _loop_task is not None:
        _loop_task.cancel()
        _loop_task = None
