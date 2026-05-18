"""
Phi accrual failure detector — adaptive peer-down detection.

Fixed-timeout failure detection (e.g. ``last_heartbeat > 15s ⇒ stale``)
has two failure modes:

  1. On a slow link, *normal* heartbeats sometimes arrive at 14.9s and
     then immediately at 15.1s — the peer flaps in/out of "stale".
  2. On a fast link with a real outage, we wait the full 15s before
     reacting, even though anything > 50ms would already be unusual.

Phi accrual (Hayashibara et al.) instead estimates a *suspicion level*
``φ`` that grows continuously with the time since the last heartbeat,
calibrated against the *observed distribution* of inter-arrival times
on this specific link:

    φ(t) = -log10(P(time_since_last ≥ t))

where P is computed from a sliding window of recent inter-arrivals.
A φ of 1 means "10% of past arrivals took at least this long",
8 means "1 in 100M". The application sets a threshold (typically 8)
and reacts when φ crosses it.

This gives:

  * **Stable detection** on noisy links — φ rises smoothly, no flap.
  * **Fast detection** on quiet links — if heartbeats normally land
    every 100ms, a 1s gap pushes φ over the threshold.
  * **Self-calibrating** — no per-link tuning needed.

Usage
-----
    detector = PhiAccrualDetector(window_size=100)
    detector.heartbeat()              # whenever a heartbeat arrives
    if detector.phi() > 8.0:
        # peer is suspected dead

The module is in-memory and per-peer keyed by ``server_id`` to avoid
two peers' inter-arrivals contaminating each other's distribution.
"""

from __future__ import annotations

import math
import statistics
import threading
import time
from collections import deque
from typing import Optional


_DEFAULT_WINDOW = 200
_DEFAULT_PHI_THRESHOLD = 8.0
_MIN_STD_DEV_MS = 50.0  # floor — avoid div-by-zero on perfectly regular links


class PhiAccrualDetector:
    """Per-peer adaptive suspicion."""

    def __init__(self, window_size: int = _DEFAULT_WINDOW) -> None:
        self._lock = threading.RLock()
        self._intervals_ms: deque[float] = deque(maxlen=window_size)
        self._last_heartbeat: float = 0.0

    def heartbeat(self, ts: Optional[float] = None) -> None:
        ts = ts if ts is not None else time.time()
        with self._lock:
            if self._last_heartbeat > 0:
                interval_ms = (ts - self._last_heartbeat) * 1000.0
                if interval_ms > 0:
                    self._intervals_ms.append(interval_ms)
            self._last_heartbeat = ts

    def phi(self, now: Optional[float] = None) -> float:
        """Current suspicion level. 0 = healthy, ≥ threshold = dead."""
        now = now if now is not None else time.time()
        with self._lock:
            if not self._intervals_ms or self._last_heartbeat == 0:
                return 0.0
            time_since_ms = (now - self._last_heartbeat) * 1000.0
            if time_since_ms <= 0:
                return 0.0
            mean = statistics.mean(self._intervals_ms)
            stddev = (
                statistics.stdev(self._intervals_ms)
                if len(self._intervals_ms) >= 2
                else _MIN_STD_DEV_MS
            )
            stddev = max(stddev, _MIN_STD_DEV_MS)
        # Probability a normal-distributed inter-arrival exceeds time_since_ms.
        # Using complementary cumulative distribution (1 - CDF).
        # erfc gives 2 × (1 - CDF) at z = (x - mu) / (sigma * sqrt(2)).
        z = (time_since_ms - mean) / (stddev * math.sqrt(2))
        prob_at_least = 0.5 * math.erfc(z)
        if prob_at_least <= 1e-15:
            return 17.0  # cap so we don't return inf for log10(0)
        return -math.log10(prob_at_least)

    def is_available(self, threshold: float = _DEFAULT_PHI_THRESHOLD) -> bool:
        return self.phi() < threshold

    def snapshot(self) -> dict:
        with self._lock:
            samples = list(self._intervals_ms)
            mean = statistics.mean(samples) if samples else 0.0
            stddev = statistics.stdev(samples) if len(samples) >= 2 else 0.0
            return {
                "samples":          len(samples),
                "mean_interval_ms": round(mean, 1),
                "stddev_ms":        round(stddev, 1),
                "phi":              round(self.phi(), 2),
                "last_heartbeat":   self._last_heartbeat,
            }


# ── Per-peer registry ───────────────────────────────────────────


class PhiRegistry:
    _singleton: "PhiRegistry | None" = None

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._detectors: dict[str, PhiAccrualDetector] = {}

    @classmethod
    def instance(cls) -> "PhiRegistry":
        if cls._singleton is None:
            cls._singleton = PhiRegistry()
        return cls._singleton

    def detector_for(self, server_id: str) -> PhiAccrualDetector:
        with self._lock:
            d = self._detectors.get(server_id)
            if d is None:
                d = PhiAccrualDetector()
                self._detectors[server_id] = d
            return d

    def heartbeat(self, server_id: str) -> None:
        self.detector_for(server_id).heartbeat()

    def is_available(
        self,
        server_id: str,
        threshold: float = _DEFAULT_PHI_THRESHOLD,
    ) -> bool:
        return self.detector_for(server_id).is_available(threshold)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "threshold": _DEFAULT_PHI_THRESHOLD,
                "peers": {
                    sid: d.snapshot()
                    for sid, d in self._detectors.items()
                },
            }

    def evict(self, server_id: str) -> None:
        with self._lock:
            self._detectors.pop(server_id, None)


def get_phi_registry() -> PhiRegistry:
    return PhiRegistry.instance()
