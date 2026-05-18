"""Topology snapshot capturer — periodic point-in-time snapshots.

The topology graph is the live structural truth (see app.topology).
This module captures a snapshot every ``snapshot_interval_sec`` and
keeps the last N in memory so operators can compare "what did the
cluster look like at 09:00?" vs. "now".

Snapshots are also emitted as ``topology.snapshot`` events for any
external archiver to consume.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from typing import Optional

from app.core.logging import get_logger
from app.monitoring.monitoring_config import get_config
from app.monitoring.monitoring_events import emit

logger = get_logger(__name__)


class TopologySnapshotCapturer:
    _singleton: "TopologySnapshotCapturer | None" = None

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._snapshots: deque = deque()
        self._loop_task: Optional[asyncio.Task] = None
        self._running = False
        self._cap = get_config().snapshot_history_max

    @classmethod
    def instance(cls) -> "TopologySnapshotCapturer":
        if cls._singleton is None:
            cls._singleton = TopologySnapshotCapturer()
        return cls._singleton

    # ── Capture ─────────────────────────────────────────────

    def capture(self) -> dict:
        try:
            from app.topology import get_topology_manager
            graph_snap = get_topology_manager().snapshot()
            stats = graph_snap.get("stats", {})
        except Exception as e:
            stats = {"error": str(e)}
            graph_snap = {}

        snap = {
            "ts":           time.time(),
            "stats":        stats,
            "node_count":   stats.get("node_count", 0),
            "link_count":   stats.get("link_count", 0),
            "components":   stats.get("components", 0),
            "bridges":      list(stats.get("bridges", [])),
        }
        with self._lock:
            self._snapshots.append(snap)
            while len(self._snapshots) > self._cap:
                self._snapshots.popleft()
        emit("topology.snapshot", {
            "ts":         snap["ts"],
            "node_count": snap["node_count"],
            "components": snap["components"],
        })
        return snap

    def latest(self) -> Optional[dict]:
        with self._lock:
            return dict(self._snapshots[-1]) if self._snapshots else None

    def history(self, limit: int = 10) -> list[dict]:
        with self._lock:
            return list(self._snapshots)[-int(limit):]

    # ── Diff ─────────────────────────────────────────────────

    def diff(self) -> Optional[dict]:
        """Compare the last two snapshots — returns None if < 2."""
        with self._lock:
            if len(self._snapshots) < 2:
                return None
            old = self._snapshots[-2]
            new = self._snapshots[-1]
        return {
            "delta_nodes":      new["node_count"] - old["node_count"],
            "delta_links":      new["link_count"] - old["link_count"],
            "delta_components": new["components"] - old["components"],
            "delta_bridges":    sorted(set(new["bridges"]) ^ set(old["bridges"])),
            "elapsed_sec":      round(new["ts"] - old["ts"], 1),
        }

    # ── Background loop ─────────────────────────────────────

    async def _run_loop(self) -> None:
        cfg = get_config()
        if not cfg.enable_snapshots:
            logger.info("monitoring_snapshots_disabled")
            return
        self._running = True
        logger.info(
            "monitoring_snapshots_started",
            interval_sec=cfg.snapshot_interval_sec,
        )
        try:
            while self._running:
                try:
                    self.capture()
                except Exception as e:
                    logger.warning("monitoring_snapshot_failed", error=str(e))
                await asyncio.sleep(cfg.snapshot_interval_sec)
        finally:
            logger.info("monitoring_snapshots_stopped")

    def start(self) -> None:
        if self._loop_task is not None and not self._loop_task.done():
            return
        try:
            loop = asyncio.get_event_loop()
            self._loop_task = loop.create_task(
                self._run_loop(), name="monitoring-snapshots",
            )
        except RuntimeError:
            logger.warning("monitoring_snapshots_no_event_loop_yet")

    def stop(self) -> None:
        self._running = False
        if self._loop_task is not None:
            self._loop_task.cancel()
            self._loop_task = None


def get_topology_capturer() -> TopologySnapshotCapturer:
    return TopologySnapshotCapturer.instance()
