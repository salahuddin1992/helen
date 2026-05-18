"""Heartbeat manager — beat the local node, feed phi accrual.

The node_registry already has a heartbeat method (called by the
control_plane tick). This manager:

  * Calls phi_accrual ``heartbeat()`` for our own node so we appear
    healthy in the failure detector for our peers.
  * Listens for ``member.joined`` events and seeds the phi detector
    for new peers.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from app.core.logging import get_logger
from app.distributed_system.distributed_config import get_config
from app.distributed_system.distributed_events import emit
from app.distributed_system.node_identity import server_id

logger = get_logger(__name__)


class HeartbeatManager:
    _singleton: "HeartbeatManager | None" = None

    def __init__(self) -> None:
        self._loop_task: Optional[asyncio.Task] = None
        self._running = False

    @classmethod
    def instance(cls) -> "HeartbeatManager":
        if cls._singleton is None:
            cls._singleton = HeartbeatManager()
        return cls._singleton

    def beat_self(self) -> None:
        try:
            from app.services.phi_accrual import get_phi_registry
            get_phi_registry().heartbeat(server_id())
        except Exception:
            pass
        emit("heartbeat.beat", {"server_id": server_id()})

    async def _run_loop(self) -> None:
        cfg = get_config()
        self._running = True
        logger.info("ds_heartbeat_started", interval_sec=cfg.heartbeat_interval_sec)
        try:
            while self._running:
                try:
                    self.beat_self()
                except Exception as e:
                    logger.warning("ds_heartbeat_failed", error=str(e))
                await asyncio.sleep(cfg.heartbeat_interval_sec)
        finally:
            logger.info("ds_heartbeat_stopped")

    def start(self) -> None:
        if self._loop_task is not None and not self._loop_task.done():
            return
        try:
            loop = asyncio.get_event_loop()
            self._loop_task = loop.create_task(
                self._run_loop(), name="ds-heartbeat",
            )
        except RuntimeError:
            logger.warning("ds_heartbeat_no_event_loop_yet")

    def stop(self) -> None:
        self._running = False
        if self._loop_task is not None:
            self._loop_task.cancel()
            self._loop_task = None


def get_heartbeat_manager() -> HeartbeatManager:
    return HeartbeatManager.instance()
