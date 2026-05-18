"""
Path health tracker — turns the relay chain from "blind random" into
"latency-aware best-path".

Two responsibilities:

1. **Path scoring** — record the latency of every successful relay,
   so the next time we pick proxies we sort them best-first instead
   of shuffling. Bridges still get priority bias because cross-subnet
   reach matters more than +5ms latency.

2. **Failed-path TTL** — record paths that just failed and skip them
   for ``failure_ttl_seconds`` so a flapping peer doesn't get retried
   on every request. Combined with the per-peer circuit breaker this
   gives two-stage backoff: per-path (30s) and per-peer (after 5
   consecutive failures, also 30s).

In-memory only — paths re-probe themselves on the next relay attempt
after the TTL expires, so a restart loses no real information.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from app.core.logging import get_logger

logger = get_logger(__name__)


# Per-path EWMA window (success_count gates moving-average smoothing).
_LATENCY_ALPHA = 0.3      # weight of newest sample (older samples decay 0.7×)
_FAILURE_TTL_SEC = 30.0   # path stays in failed-set for this long
_HEALTH_TTL_SEC = 300.0   # health record evicted after 5 min idle


@dataclass
class PathHealth:
    """Per-(host, port) liveness signal."""
    latency_ms_ewma: float = 0.0
    samples: int = 0
    last_success: float = 0.0
    last_failure: float = 0.0
    fail_count: int = 0


class PathHealthTracker:
    """Singleton — keys on ``f"{host}:{port}"`` so multiple peers
    sharing a host don't pollute each other's stats."""

    _singleton: "PathHealthTracker | None" = None

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._health: dict[str, PathHealth] = {}
        self._failed_until: dict[str, float] = {}

    @classmethod
    def instance(cls) -> "PathHealthTracker":
        if cls._singleton is None:
            cls._singleton = PathHealthTracker()
        return cls._singleton

    @staticmethod
    def _key(host: str, port: int) -> str:
        return f"{host}:{int(port)}"

    def record_success(self, host: str, port: int, latency_ms: float) -> None:
        """Update EWMA latency and clear failure TTL."""
        k = self._key(host, port)
        with self._lock:
            h = self._health.setdefault(k, PathHealth())
            if h.samples == 0:
                h.latency_ms_ewma = latency_ms
            else:
                h.latency_ms_ewma = (
                    _LATENCY_ALPHA * latency_ms
                    + (1.0 - _LATENCY_ALPHA) * h.latency_ms_ewma
                )
            h.samples += 1
            h.last_success = time.time()
            h.fail_count = 0
            self._failed_until.pop(k, None)

    def record_failure(self, host: str, port: int) -> None:
        """Mark path as failed; subsequent ``is_failed`` returns True
        until the TTL expires."""
        k = self._key(host, port)
        with self._lock:
            h = self._health.setdefault(k, PathHealth())
            h.last_failure = time.time()
            h.fail_count += 1
            self._failed_until[k] = time.time() + _FAILURE_TTL_SEC

    def is_failed(self, host: str, port: int) -> bool:
        """True if path is inside the failure-cooldown window."""
        k = self._key(host, port)
        with self._lock:
            t = self._failed_until.get(k)
            if t is None:
                return False
            if time.time() >= t:
                self._failed_until.pop(k, None)
                return False
            return True

    def latency_score(self, host: str, port: int) -> float:
        """Higher = better. Untouched paths score 1.0 (optimistic so a
        new peer gets a chance); known fast paths approach 2.0; slow
        paths approach 0.

        score = 1 / (1 + latency_ms / 50)   →   0ms = 1.0, 50ms = 0.5,
                                                200ms = 0.2, 1000ms = 0.05
        Boost x2 for paths with samples ≥ 3 (proven), x0 for is_failed.
        """
        if self.is_failed(host, port):
            return 0.0
        k = self._key(host, port)
        with self._lock:
            h = self._health.get(k)
            if h is None or h.samples == 0:
                return 1.0
            base = 1.0 / (1.0 + h.latency_ms_ewma / 50.0)
            if h.samples >= 3:
                base *= 2.0
            return base

    def snapshot(self) -> dict:
        """Diagnostic — used by /api/admin/cluster/path-health."""
        with self._lock:
            now = time.time()
            return {
                "paths": [
                    {
                        "key": k,
                        "latency_ms": round(h.latency_ms_ewma, 2),
                        "samples": h.samples,
                        "last_success_age_s": round(now - h.last_success, 1)
                            if h.last_success else None,
                        "last_failure_age_s": round(now - h.last_failure, 1)
                            if h.last_failure else None,
                        "fail_count": h.fail_count,
                        "in_cooldown": k in self._failed_until,
                    }
                    for k, h in self._health.items()
                ],
                "failed_count": len(self._failed_until),
                "tracked_count": len(self._health),
            }

    def evict_stale(self) -> int:
        """Drop entries older than HEALTH_TTL. Called periodically."""
        cutoff = time.time() - _HEALTH_TTL_SEC
        with self._lock:
            dead = [
                k for k, h in self._health.items()
                if max(h.last_success, h.last_failure) < cutoff
            ]
            for k in dead:
                self._health.pop(k, None)
            return len(dead)


def get_path_health() -> PathHealthTracker:
    return PathHealthTracker.instance()
