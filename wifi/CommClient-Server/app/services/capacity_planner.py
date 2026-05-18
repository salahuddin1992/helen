"""Capacity planner — trend-based forecast of when caps will saturate.

Walks the local node's capacity (sockets / rooms / audio / video /
broadcast) and the live load. From a small rolling window of the
load values, fits a linear trend and projects when each cap will
hit 80% / 90% / 100%.

This is *forecasting*, not real-time alerting (that lives in
``alert_manager``). Operators consult the planner when sizing the
cluster for the next quarter / event / launch.

Output shape::

    {
      "capacity": { ... advertised limits ... },
      "current":  { sockets: 1234, rooms: 5, ... },
      "utilization_pct": { sockets: 2.9, rooms: 3.0, ... },
      "trend_per_hour":  { sockets: +120, rooms: +0.1, ... },
      "saturation_eta": {
          "sockets": { "80%_hours": 42.3, "100%_hours": 53.1 },
          ...
      }
    }
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Optional


SAMPLE_INTERVAL_SEC = 60.0          # we sample once per minute
WINDOW_SAMPLES = 1440               # 24 h × 60 min — one full day of data


class CapacityPlanner:
    _singleton: "CapacityPlanner | None" = None

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # name -> deque of (ts, value)
        self._series: dict[str, deque] = {}

    @classmethod
    def instance(cls) -> "CapacityPlanner":
        if cls._singleton is None:
            cls._singleton = CapacityPlanner()
        return cls._singleton

    # ── Sampling ──────────────────────────────────────────

    def record(self, metric: str, value: float) -> None:
        ts = time.time()
        with self._lock:
            dq = self._series.get(metric)
            if dq is None:
                dq = deque(maxlen=WINDOW_SAMPLES)
                self._series[metric] = dq
            dq.append((ts, float(value)))

    def _sample_now(self) -> dict:
        """Pull live load values from node_registry self entry."""
        try:
            from app.services.node_registry import get_registry
            self_node = next(
                (n for n in get_registry().nodes(include_dead=True)
                 if n.self_node),
                None,
            )
            if self_node is None:
                return {}
            load = self_node.load
            return {
                "sockets":      float(load.active_sockets),
                "rooms":        float(load.active_rooms),
                "calls":        float(load.active_calls),
                "cpu_pct":      float(load.cpu_pct),
                "rss_pct":      float(load.rss_pct),
            }
        except Exception:
            return {}

    def tick(self) -> dict:
        sample = self._sample_now()
        for k, v in sample.items():
            self.record(k, v)
        return sample

    # ── Forecast ──────────────────────────────────────────

    def _trend_per_hour(self, metric: str) -> Optional[float]:
        """Simple least-squares slope on the rolling window;
        returns delta per hour."""
        with self._lock:
            samples = list(self._series.get(metric) or [])
        if len(samples) < 5:
            return None
        # Normalise time to hours since first sample.
        t0 = samples[0][0]
        xs = [(t - t0) / 3600.0 for t, _ in samples]
        ys = [v for _, v in samples]
        n = len(xs)
        mx = sum(xs) / n
        my = sum(ys) / n
        num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        den = sum((x - mx) ** 2 for x in xs)
        if den <= 1e-9:
            return 0.0
        return num / den

    def _eta_to(self, current: float, target: float,
               trend_per_hour: Optional[float]) -> Optional[float]:
        if trend_per_hour is None or trend_per_hour <= 0:
            return None
        if current >= target:
            return 0.0
        return (target - current) / trend_per_hour

    def forecast(self) -> dict:
        try:
            from app.services.node_registry import get_registry
            self_node = next(
                (n for n in get_registry().nodes(include_dead=True)
                 if n.self_node),
                None,
            )
        except Exception:
            self_node = None
        if self_node is None:
            return {"ok": False, "error": "no_self_node"}

        cap = self_node.capacity
        load = self_node.load

        # Map capacity ↔ load names. CPU + RSS treat 100% as the cap.
        pairs = [
            ("sockets",  cap.max_concurrent_sockets, float(load.active_sockets)),
            ("rooms",    cap.max_concurrent_rooms,   float(load.active_rooms)),
            ("calls",    cap.max_audio_participants, float(load.active_calls)),
            ("cpu_pct",  100,                        float(load.cpu_pct)),
            ("rss_pct",  100,                        float(load.rss_pct)),
        ]

        out: dict = {
            "capacity":         {},
            "current":          {},
            "utilization_pct":  {},
            "trend_per_hour":   {},
            "saturation_eta":   {},
        }
        for name, ceiling, current in pairs:
            ceiling = max(1, int(ceiling))
            out["capacity"][name]    = ceiling
            out["current"][name]     = current
            out["utilization_pct"][name] = round(100.0 * current / ceiling, 2)
            trend = self._trend_per_hour(name)
            out["trend_per_hour"][name] = (
                round(trend, 3) if trend is not None else None
            )
            out["saturation_eta"][name] = {
                "80%_hours":  self._eta_round(current, ceiling * 0.8, trend),
                "90%_hours":  self._eta_round(current, ceiling * 0.9, trend),
                "100%_hours": self._eta_round(current, ceiling * 1.0, trend),
            }
        out["ok"] = True
        return out

    @staticmethod
    def _eta_round(current: float, target: float,
                   trend: Optional[float]) -> Optional[float]:
        if trend is None or trend <= 0:
            return None
        if current >= target:
            return 0.0
        eta = (target - current) / trend
        return round(eta, 2)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "metrics_tracked": sorted(self._series.keys()),
                "samples_count": {
                    k: len(v) for k, v in self._series.items()
                },
                "window":         WINDOW_SAMPLES,
                "interval_sec":   SAMPLE_INTERVAL_SEC,
            }


def get_capacity_planner() -> CapacityPlanner:
    return CapacityPlanner.instance()
