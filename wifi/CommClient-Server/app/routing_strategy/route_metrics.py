"""Route-strategy metrics — counters + per-strategy timing.

The manager calls ``record_decision`` for every routing call; the
counters drive the admin dashboards and the prometheus exporter.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from typing import Optional


class StrategyMetrics:
    _singleton: "StrategyMetrics | None" = None

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._counters: dict[str, int] = defaultdict(int)
        self._latency_ewma_ms: float = 0.0
        self._latency_samples: int = 0
        self._strategy_calls: dict[str, int] = defaultdict(int)
        self._strategy_total_ms: dict[str, float] = defaultdict(float)
        self._last_decision_at: float = 0.0

    @classmethod
    def instance(cls) -> "StrategyMetrics":
        if cls._singleton is None:
            cls._singleton = StrategyMetrics()
        return cls._singleton

    # ── Counters ────────────────────────────────────────────

    def incr(self, name: str, delta: int = 1) -> None:
        with self._lock:
            self._counters[name] += delta

    def record_decision(
        self, *, has_route: bool, duration_ms: float,
        primary_route_type: Optional[str] = None,
    ) -> None:
        with self._lock:
            self._counters["decisions_total"] += 1
            if has_route:
                self._counters["decisions_resolved"] += 1
            else:
                self._counters["decisions_unresolved"] += 1
            if primary_route_type:
                self._counters[f"primary_route_type_{primary_route_type}"] += 1
            if self._latency_samples == 0:
                self._latency_ewma_ms = duration_ms
            else:
                self._latency_ewma_ms = (
                    0.3 * duration_ms + 0.7 * self._latency_ewma_ms
                )
            self._latency_samples += 1
            self._last_decision_at = time.time()

    def record_strategy(self, strategy: str, duration_ms: float) -> None:
        with self._lock:
            self._strategy_calls[strategy] += 1
            self._strategy_total_ms[strategy] += duration_ms

    # ── Snapshots ───────────────────────────────────────────

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "counters":          dict(self._counters),
                "decisions_latency_ms_ewma": round(self._latency_ewma_ms, 3),
                "decisions_samples": self._latency_samples,
                "last_decision_at":  self._last_decision_at,
                "strategy_avg_ms":   {
                    name: round(self._strategy_total_ms[name] /
                                max(1, self._strategy_calls[name]), 3)
                    for name in self._strategy_calls
                },
                "strategy_calls":    dict(self._strategy_calls),
            }


def get_metrics() -> StrategyMetrics:
    return StrategyMetrics.instance()
