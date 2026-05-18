"""
Phase 6 / Module AC — Cluster node registry.

Each Helen-Server instance registers itself in ``cluster_nodes`` on
startup, heartbeats every 5s, and reaps stale rows after 60s.

The data model is **eventually consistent**. Callers that need a strong
single-truth (e.g. leader election) should use
``app.services.cluster.leader_election`` instead.
"""
from __future__ import annotations

import asyncio
import os
import socket
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.session import async_session_factory
from app.models.cluster import ClusterNode
from app.services.cluster.leader_election import _resolve_node_id

logger = get_logger(__name__)

HEARTBEAT_INTERVAL = 5.0
STALE_AFTER_SECONDS = 60
VERSION = os.environ.get("HELEN_VERSION", "0.0.0-dev")


class NodeRegistry:
    """Self-registers + heartbeats. Cluster-wide visibility via DB."""

    def __init__(
        self,
        *,
        node_id: Optional[str] = None,
        hostname: Optional[str] = None,
        advertise_url: Optional[str] = None,
        capabilities: Optional[dict[str, Any]] = None,
    ) -> None:
        self.node_id = node_id or _resolve_node_id()
        self.hostname = hostname or socket.gethostname()
        # advertise_url: priority order
        #   1. HELEN_ADVERTISE_URL env
        #   2. constructed from HOST/PORT
        s = get_settings()
        self.advertise_url = (
            advertise_url
            or os.environ.get("HELEN_ADVERTISE_URL")
            or f"http://{s.HOST}:{s.PORT}"
        )
        self.capabilities = capabilities or {}
        self._task: Optional[asyncio.Task[None]] = None
        self._reaper_task: Optional[asyncio.Task[None]] = None
        self._stop = asyncio.Event()
        self._draining = False

    # ── public API ──────────────────────────────────────────

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        await self._upsert(status="joining")
        # transition to active right after upsert
        await self._upsert(status="active")
        self._task = asyncio.create_task(self._heartbeat_loop(), name="cluster-heartbeat")
        self._reaper_task = asyncio.create_task(self._reaper_loop(), name="cluster-reaper")
        logger.info(
            "node_registry: registered node=%s host=%s url=%s",
            self.node_id[:12], self.hostname, self.advertise_url,
        )

    async def stop(self) -> None:
        self._stop.set()
        for t in (self._task, self._reaper_task):
            if t is None:
                continue
            try:
                await asyncio.wait_for(t, timeout=HEARTBEAT_INTERVAL + 2)
            except asyncio.TimeoutError:                            # pragma: no cover
                t.cancel()
        # mark self down so other nodes don't keep us in the routing table
        try:
            await self._upsert(status="down")
        except Exception:                                           # pragma: no cover
            pass

    async def drain(self) -> None:
        """Mark this node as draining so the load-balancer stops sending it
        new connections. The node keeps running its existing workload."""
        self._draining = True
        await self._upsert(status="draining")
        logger.warning("node_registry: node %s draining", self.node_id[:12])

    async def get_active_nodes(self) -> list[ClusterNode]:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=STALE_AFTER_SECONDS)
        async with async_session_factory() as db:
            res = await db.execute(
                select(ClusterNode)
                .where(ClusterNode.status.in_(("active", "draining")))
                .where(ClusterNode.last_seen >= cutoff)
                .order_by(ClusterNode.joined_at)
            )
            return list(res.scalars().all())

    async def get_all_nodes(self) -> list[ClusterNode]:
        async with async_session_factory() as db:
            res = await db.execute(select(ClusterNode).order_by(ClusterNode.joined_at))
            return list(res.scalars().all())

    async def remove_node(self, node_id: str) -> bool:
        async with async_session_factory() as db:
            row = (await db.execute(
                select(ClusterNode).where(ClusterNode.node_id == node_id)
            )).scalar_one_or_none()
            if row is None:
                return False
            await db.delete(row)
            await db.commit()
            return True

    async def set_status(self, node_id: str, status: str) -> bool:
        async with async_session_factory() as db:
            res = await db.execute(
                update(ClusterNode)
                .where(ClusterNode.node_id == node_id)
                .values(status=status)
            )
            await db.commit()
            return (res.rowcount or 0) > 0

    # ── internals ───────────────────────────────────────────

    async def _upsert(self, *, status: str) -> None:
        now = datetime.now(timezone.utc)
        async with async_session_factory() as db:  # type: AsyncSession
            row = (await db.execute(
                select(ClusterNode).where(ClusterNode.node_id == self.node_id)
            )).scalar_one_or_none()
            if row is None:
                row = ClusterNode(
                    node_id=self.node_id,
                    hostname=self.hostname,
                    advertise_url=self.advertise_url,
                    status=status,
                    role="replica",
                    version=VERSION,
                    joined_at=now,
                    last_seen=now,
                    capabilities=self.capabilities,
                )
                db.add(row)
            else:
                row.hostname = self.hostname
                row.advertise_url = self.advertise_url
                row.status = status if not self._draining else "draining"
                row.version = VERSION
                row.last_seen = now
                row.capabilities = self.capabilities
            try:
                await db.commit()
            except Exception as exc:                                # pragma: no cover
                logger.warning("node_registry: upsert failed (%s)", exc)
                await db.rollback()

    async def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._upsert(status="draining" if self._draining else "active")
            except Exception as exc:                                # pragma: no cover
                logger.warning("node_registry: heartbeat err (%s)", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=HEARTBEAT_INTERVAL)
            except asyncio.TimeoutError:
                continue

    async def _reaper_loop(self) -> None:
        """Periodic stale-node sweep — runs on every node but is idempotent."""
        while not self._stop.is_set():
            try:
                cutoff = datetime.now(timezone.utc) - timedelta(seconds=STALE_AFTER_SECONDS)
                async with async_session_factory() as db:
                    res = await db.execute(
                        select(ClusterNode).where(ClusterNode.last_seen < cutoff)
                        .where(ClusterNode.node_id != self.node_id)
                    )
                    for row in res.scalars().all():
                        if row.status != "down":
                            row.status = "down"
                    await db.commit()
            except Exception as exc:                                # pragma: no cover
                logger.warning("node_registry: reaper err (%s)", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=HEARTBEAT_INTERVAL * 3)
            except asyncio.TimeoutError:
                continue


# ── singleton ───────────────────────────────────────────────


_singleton: Optional[NodeRegistry] = None


def get_node_registry() -> NodeRegistry:
    global _singleton
    if _singleton is None:
        _singleton = NodeRegistry()
    return _singleton
