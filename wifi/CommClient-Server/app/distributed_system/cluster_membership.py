"""Cluster membership — JOIN / LEAVE / EVICT events.

The membership manager:

  * Polls ``node_registry`` every ``MEMBER_CHECK_SEC``.
  * Detects newly-fresh peers (JOIN), newly-stale peers (LEAVE),
    and dead peers (EVICT).
  * Emits ``member.joined`` / ``member.left`` / ``member.evicted``
    events for downstream listeners.

Idempotent — re-running the same cycle on a stable cluster emits
nothing.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Optional

from app.core.logging import get_logger
from app.distributed_system import node_registry as ds_registry
from app.distributed_system.distributed_config import get_config
from app.distributed_system.distributed_events import emit

logger = get_logger(__name__)


class ClusterMembership:
    _singleton: "ClusterMembership | None" = None

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._known: set[str] = set()
        self._fresh: set[str] = set()
        self._loop_task: Optional[asyncio.Task] = None
        self._running = False

    @classmethod
    def instance(cls) -> "ClusterMembership":
        if cls._singleton is None:
            cls._singleton = ClusterMembership()
        return cls._singleton

    def members(self) -> set[str]:
        with self._lock:
            return set(self._fresh)

    def all_known(self) -> set[str]:
        with self._lock:
            return set(self._known)

    def check_once(self) -> dict:
        nodes = ds_registry.list_all(include_dead=True)
        now_known: set[str] = set()
        now_fresh: set[str] = set()
        for n in nodes:
            sid = n.get("node_id")
            if not sid:
                continue
            now_known.add(sid)
            if n.get("fresh") and not n.get("dead"):
                now_fresh.add(sid)

        with self._lock:
            joined = now_fresh - self._fresh
            left   = self._fresh - now_fresh
            evicted = self._known - now_known
            self._known = now_known
            self._fresh = now_fresh

        for sid in joined:
            emit("member.joined", {"node_id": sid})
        for sid in left:
            emit("member.left", {"node_id": sid})
        for sid in evicted:
            emit("member.evicted", {"node_id": sid})

        return {
            "joined":  sorted(joined),
            "left":    sorted(left),
            "evicted": sorted(evicted),
            "fresh":   len(now_fresh),
            "known":   len(now_known),
        }

    async def _run_loop(self) -> None:
        cfg = get_config()
        self._running = True
        logger.info("ds_membership_started", interval_sec=cfg.membership_check_sec)
        try:
            while self._running:
                try:
                    self.check_once()
                except Exception as e:
                    logger.warning("ds_membership_cycle_failed", error=str(e))
                await asyncio.sleep(cfg.membership_check_sec)
        finally:
            logger.info("ds_membership_stopped")

    def start(self) -> None:
        if self._loop_task is not None and not self._loop_task.done():
            return
        try:
            loop = asyncio.get_event_loop()
            self._loop_task = loop.create_task(
                self._run_loop(), name="ds-membership",
            )
        except RuntimeError:
            logger.warning("ds_membership_no_event_loop_yet")

    def stop(self) -> None:
        self._running = False
        if self._loop_task is not None:
            self._loop_task.cancel()
            self._loop_task = None


def get_cluster_membership() -> ClusterMembership:
    return ClusterMembership.instance()
