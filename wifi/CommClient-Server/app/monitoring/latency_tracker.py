"""Latency tracker — bounded request-latency histograms.

Per ``op_name`` we track the rolling latency window (default 500
samples) and expose:

  * count
  * mean / median / p95 / p99
  * min / max

Pure data structure — no I/O, callable from any code path.

    from app.monitoring.latency_tracker import time_op
    with time_op("api.x"):
        await do_work()
"""

from __future__ import annotations

import contextlib
import statistics
import threading
import time
from collections import deque
from typing import Iterator

from app.monitoring.monitoring_config import get_config


class LatencyTracker:
    _singleton: "LatencyTracker | None" = None

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._windows: dict[str, deque] = {}

    @classmethod
    def instance(cls) -> "LatencyTracker":
        if cls._singleton is None:
            cls._singleton = LatencyTracker()
        return cls._singleton

    # ── Recording ───────────────────────────────────────────

    def record(self, op: str, ms: float) -> None:
        cfg = get_config()
        with self._lock:
            dq = self._windows.get(op)
            if dq is None:
                dq = deque(maxlen=cfg.latency_window)
                self._windows[op] = dq
            dq.append(float(ms))

    # ── Reading ─────────────────────────────────────────────

    @staticmethod
    def _percentile(samples: list[float], pct: float) -> float:
        if not samples:
            return 0.0
        s = sorted(samples)
        idx = max(0, min(len(s) - 1, int(pct / 100.0 * len(s))))
        return s[idx]

    def stats(self, op: str) -> dict:
        with self._lock:
            dq = self._windows.get(op)
            samples = list(dq) if dq else []
        if not samples:
            return {"op": op, "count": 0}
        return {
            "op":     op,
            "count":  len(samples),
            "mean":   round(statistics.mean(samples), 3),
            "median": round(statistics.median(samples), 3),
            "p95":    round(self._percentile(samples, 95), 3),
            "p99":    round(self._percentile(samples, 99), 3),
            "min":    round(min(samples), 3),
            "max":    round(max(samples), 3),
        }

    def all_stats(self) -> dict:
        with self._lock:
            ops = list(self._windows.keys())
        return {op: self.stats(op) for op in ops}

    def reset(self, op: str | None = None) -> None:
        with self._lock:
            if op is None:
                self._windows.clear()
            else:
                self._windows.pop(op, None)


def get_latency_tracker() -> LatencyTracker:
    return LatencyTracker.instance()


# ── Convenience context manager ────────────────────────────────


@contextlib.contextmanager
def time_op(op: str) -> Iterator[None]:
    """Context manager that records elapsed milliseconds for ``op``.

        with time_op("api.fetch_user"):
            data = await db.fetch_one(...)
    """
    t0 = time.time()
    try:
        yield
    finally:
        get_latency_tracker().record(op, (time.time() - t0) * 1000.0)
