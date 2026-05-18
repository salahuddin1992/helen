"""Metrics collector — aggregates counters from every subsystem.

Reads from path_health, trust_score, partition_detector,
backpressure, multipath_router, distributed_system on a schedule
and rolls them up into one ``snapshot()`` blob. The Prometheus
exporter (``services.metrics_export``) is unaffected — this module
is the higher-level *observability* aggregator while the exporter
stays the wire-format renderer.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Optional

from app.core.logging import get_logger
from app.monitoring.monitoring_config import get_config

logger = get_logger(__name__)


class MetricsCollector:
    _singleton: "MetricsCollector | None" = None

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._latest: dict = {}
        self._collected_at: float = 0.0
        self._loop_task: Optional[asyncio.Task] = None
        self._running = False

    @classmethod
    def instance(cls) -> "MetricsCollector":
        if cls._singleton is None:
            cls._singleton = MetricsCollector()
        return cls._singleton

    # ── Per-subsystem readers ───────────────────────────────

    @staticmethod
    def _path_health() -> dict:
        try:
            from app.services.path_health import get_path_health
            return get_path_health().snapshot()
        except Exception:
            return {}

    @staticmethod
    def _backpressure() -> dict:
        try:
            from app.services.backpressure import get_backpressure
            return get_backpressure().snapshot()
        except Exception:
            return {}

    @staticmethod
    def _partition() -> dict:
        try:
            from app.services.partition_detector import get_partition_state
            return get_partition_state().snapshot()
        except Exception:
            return {}

    @staticmethod
    def _multipath() -> dict:
        try:
            from app.services.multipath_router import snapshot as mp_snapshot
            return mp_snapshot()
        except Exception:
            return {}

    @staticmethod
    def _routing_strategy() -> dict:
        try:
            from app.routing_strategy import get_strategy_manager
            return get_strategy_manager().snapshot().get("metrics", {})
        except Exception:
            return {}

    @staticmethod
    def _distributed() -> dict:
        try:
            from app.distributed_system import get_distributed_manager
            return get_distributed_manager().snapshot().get("cluster", {})
        except Exception:
            return {}

    # ── Collect ─────────────────────────────────────────────

    def collect_once(self) -> dict:
        snapshot = {
            "ts":              time.time(),
            "path_health":     self._path_health(),
            "backpressure":    self._backpressure(),
            "partition":       self._partition(),
            "multipath":       self._multipath(),
            "routing_strategy": self._routing_strategy(),
            "distributed":     self._distributed(),
        }
        with self._lock:
            self._latest = snapshot
            self._collected_at = snapshot["ts"]
        return snapshot

    def latest(self) -> dict:
        with self._lock:
            return dict(self._latest)

    def collected_at(self) -> float:
        with self._lock:
            return self._collected_at

    # ── Background loop ─────────────────────────────────────

    async def _run_loop(self) -> None:
        cfg = get_config()
        self._running = True
        logger.info(
            "monitoring_metrics_started",
            interval_sec=cfg.metrics_collect_interval_sec,
        )
        try:
            while self._running:
                try:
                    self.collect_once()
                except Exception as e:
                    logger.warning(
                        "monitoring_metrics_failed", error=str(e),
                    )
                await asyncio.sleep(cfg.metrics_collect_interval_sec)
        finally:
            logger.info("monitoring_metrics_stopped")

    def start(self) -> None:
        if self._loop_task is not None and not self._loop_task.done():
            return
        try:
            loop = asyncio.get_event_loop()
            self._loop_task = loop.create_task(
                self._run_loop(), name="monitoring-metrics",
            )
        except RuntimeError:
            logger.warning("monitoring_metrics_no_event_loop_yet")

    def stop(self) -> None:
        self._running = False
        if self._loop_task is not None:
            self._loop_task.cancel()
            self._loop_task = None


def get_metrics_collector() -> MetricsCollector:
    return MetricsCollector.instance()
