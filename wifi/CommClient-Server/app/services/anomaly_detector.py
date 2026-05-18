"""Anomaly detector — z-score over rolling metric windows.

Threshold-based alerts (in monitoring.alert_manager) catch crossings
of fixed lines (e.g. CPU > 90%). They miss *unusual* patterns that
are still under those thresholds — like a slow rise in failed-relay
rate that hasn't yet triggered.

This detector keeps a rolling window of recent values per metric and
emits ``anomaly.detected`` when |z-score| > ``Z_THRESHOLD``. The
window auto-trims so old samples don't poison the baseline.

Default thresholds (env-tunable):

  WINDOW_SIZE  = 100
  Z_THRESHOLD  = 3.0
  CHECK_INTERVAL_SEC = 30.0

Metrics tracked by default:

  * cpu_pct           — control plane CPU
  * rss_pct           — RSS memory percent
  * active_sockets    — live WS count
  * retry_queue_depth — pending retries
  * partition_count   — connected components
"""

from __future__ import annotations

import asyncio
import math
import os
import statistics
import threading
import time
from collections import deque
from typing import Callable, Optional

from app.core.logging import get_logger
from app.monitoring.monitoring_events import emit

logger = get_logger(__name__)


def _i(env: str, default: int) -> int:
    try:
        return int(os.environ.get(env) or default)
    except (TypeError, ValueError):
        return default


def _f(env: str, default: float) -> float:
    try:
        return float(os.environ.get(env) or default)
    except (TypeError, ValueError):
        return default


WINDOW_SIZE         = _i("HELEN_ANOMALY_WINDOW", 100)
Z_THRESHOLD         = _f("HELEN_ANOMALY_Z", 3.0)
CHECK_INTERVAL_SEC  = _f("HELEN_ANOMALY_CHECK_SEC", 30.0)


# Probe signature: () → numeric value (or None to skip).
ProbeFn = Callable[[], Optional[float]]


# ── Built-in probes ─────────────────────────────────────────────


def _cpu_pct() -> Optional[float]:
    try:
        from app.services.control_plane import ControlPlane
        s = ControlPlane.instance().status()
        return float(s["inputs"].get("cpu_p95") or 0.0)
    except Exception:
        return None


def _rss_pct() -> Optional[float]:
    try:
        from app.services.control_plane import ControlPlane
        s = ControlPlane.instance().status()
        return float(s["inputs"].get("rss_p95") or 0.0)
    except Exception:
        return None


def _active_sockets() -> Optional[float]:
    try:
        from app.services.control_plane import ControlPlane
        s = ControlPlane.instance().status()
        return float(s["inputs"].get("active_sockets") or 0)
    except Exception:
        return None


def _retry_queue_depth() -> Optional[float]:
    try:
        from app.services.replication_manager import _store
        return float(len(_store().all_keys()))
    except Exception:
        return None


def _partition_count() -> Optional[float]:
    try:
        from app.services.partition_detector import get_partition_state
        snap = get_partition_state().snapshot()
        return float(snap.get("high_water", 1) - snap.get("fresh_count", 0))
    except Exception:
        return None


_DEFAULT_PROBES: dict[str, ProbeFn] = {
    "cpu_pct":           _cpu_pct,
    "rss_pct":           _rss_pct,
    "active_sockets":    _active_sockets,
    "retry_queue_depth": _retry_queue_depth,
    "partition_lag":     _partition_count,
}


# ── Detector singleton ──────────────────────────────────────────


class AnomalyDetector:
    _singleton: "AnomalyDetector | None" = None

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._probes: dict[str, ProbeFn] = dict(_DEFAULT_PROBES)
        self._windows: dict[str, deque] = {
            name: deque(maxlen=WINDOW_SIZE) for name in self._probes
        }
        self._last_anomalies: dict[str, dict] = {}
        self._loop_task: Optional[asyncio.Task] = None
        self._running = False

    @classmethod
    def instance(cls) -> "AnomalyDetector":
        if cls._singleton is None:
            cls._singleton = AnomalyDetector()
        return cls._singleton

    # ── Probe registration ─────────────────────────────────

    def register(self, name: str, fn: ProbeFn) -> None:
        with self._lock:
            self._probes[name] = fn
            if name not in self._windows:
                self._windows[name] = deque(maxlen=WINDOW_SIZE)

    # ── Sampling + detection ───────────────────────────────

    def sample_once(self) -> dict:
        with self._lock:
            probes = dict(self._probes)
        results: dict[str, dict] = {}
        for name, fn in probes.items():
            try:
                v = fn()
            except Exception:
                v = None
            if v is None:
                continue
            with self._lock:
                w = self._windows[name]
                w.append(float(v))
                samples = list(w)
            entry = self._evaluate(name, float(v), samples)
            results[name] = entry
        return results

    def _evaluate(self, name: str, value: float,
                  samples: list[float]) -> dict:
        if len(samples) < 10:
            return {"value": value, "z": None, "anomaly": False,
                    "samples": len(samples)}
        mean = statistics.mean(samples)
        try:
            sd = statistics.stdev(samples)
        except statistics.StatisticsError:
            sd = 0.0
        if sd <= 1e-9:
            z = 0.0
        else:
            z = (value - mean) / sd
        is_anomaly = abs(z) >= Z_THRESHOLD
        result = {
            "value":   round(value, 4),
            "mean":    round(mean, 4),
            "stdev":   round(sd, 4),
            "z":       round(z, 3),
            "anomaly": is_anomaly,
            "samples": len(samples),
        }
        if is_anomaly:
            with self._lock:
                last = self._last_anomalies.get(name) or {}
                self._last_anomalies[name] = {**result, "ts": time.time()}
            # Don't spam — only emit when transitioning into anomaly.
            if not last.get("anomaly"):
                emit("anomaly.detected", {
                    "metric": name, **result,
                })
                logger.warning("anomaly_detected", metric=name, **result)
        else:
            with self._lock:
                if (self._last_anomalies.get(name) or {}).get("anomaly"):
                    self._last_anomalies[name] = {**result, "ts": time.time()}
                    emit("anomaly.cleared", {"metric": name, **result})
        return result

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "metrics":         sorted(self._probes.keys()),
                "window_size":     WINDOW_SIZE,
                "z_threshold":     Z_THRESHOLD,
                "last_anomalies":  dict(self._last_anomalies),
                "samples_count": {
                    name: len(w) for name, w in self._windows.items()
                },
            }

    # ── Background loop ───────────────────────────────────

    async def _run_loop(self) -> None:
        self._running = True
        logger.info("anomaly_detector_started",
                    interval_sec=CHECK_INTERVAL_SEC)
        try:
            while self._running:
                try:
                    self.sample_once()
                except Exception as e:
                    logger.warning("anomaly_sample_failed", error=str(e))
                await asyncio.sleep(CHECK_INTERVAL_SEC)
        finally:
            logger.info("anomaly_detector_stopped")

    def start(self) -> None:
        if self._loop_task is not None and not self._loop_task.done():
            return
        try:
            loop = asyncio.get_event_loop()
            self._loop_task = loop.create_task(
                self._run_loop(), name="anomaly-detector",
            )
        except RuntimeError:
            logger.warning("anomaly_detector_no_event_loop_yet")

    def stop(self) -> None:
        self._running = False
        if self._loop_task is not None:
            self._loop_task.cancel()
            self._loop_task = None


def get_anomaly_detector() -> AnomalyDetector:
    return AnomalyDetector.instance()
