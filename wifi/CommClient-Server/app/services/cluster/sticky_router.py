"""
Phase 6 / Module AC — Consistent-hashing sticky router.

Socket.io sessions are sticky by ``user_id`` so that the same user always
lands on the same node when possible. We use a virtual-node consistent
hash ring (160 vnodes / physical node) for low rebalance churn on
membership changes.

The router can also emit a snapshot of the routing table for nginx /
HAProxy to consume (via the admin API).
"""
from __future__ import annotations

import asyncio
import bisect
import hashlib
import threading
from dataclasses import dataclass, field
from typing import Iterable, Optional

from app.core.logging import get_logger
from app.services.cluster.node_registry import (
    NodeRegistry,
    get_node_registry,
)

logger = get_logger(__name__)


VNODE_REPLICAS = 160


@dataclass
class _RingEntry:
    hash_val: int
    node_id: str


@dataclass
class RingSnapshot:
    nodes: list[str] = field(default_factory=list)
    advertise_urls: dict[str, str] = field(default_factory=dict)
    points: list[tuple[int, str]] = field(default_factory=list)


def _hash(s: str) -> int:
    return int(hashlib.blake2b(s.encode("utf-8"), digest_size=8).hexdigest(), 16)


class StickyRouter:
    """Consistent-hash ring over active cluster nodes.

    Thread-safe and asyncio-safe (mutations behind an asyncio.Lock, lookups
    use the immutable snapshot pattern so they're lock-free)."""

    def __init__(
        self,
        *,
        registry: Optional[NodeRegistry] = None,
        vnodes: int = VNODE_REPLICAS,
    ) -> None:
        self._registry = registry or get_node_registry()
        self._vnodes = vnodes
        self._lock = asyncio.Lock()
        self._snapshot = RingSnapshot()
        self._auto_task: Optional[asyncio.Task[None]] = None
        self._stop = asyncio.Event()

    # ── public API ──────────────────────────────────────────

    async def start(self, refresh_interval: float = 10.0) -> None:
        if self._auto_task is not None:
            return
        self._stop.clear()
        await self.rebalance_on_node_change()
        self._auto_task = asyncio.create_task(
            self._refresh_loop(refresh_interval), name="sticky-router-refresh",
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._auto_task is not None:
            try:
                await asyncio.wait_for(self._auto_task, timeout=2.0)
            except asyncio.TimeoutError:                            # pragma: no cover
                self._auto_task.cancel()

    def route_user_to_node(self, user_id: str) -> Optional[str]:
        """Return the node_id that owns ``user_id`` per the ring, or None
        if the ring is empty."""
        snap = self._snapshot
        if not snap.points:
            return None
        h = _hash(f"user:{user_id}")
        keys = [p[0] for p in snap.points]
        idx = bisect.bisect_right(keys, h) % len(keys)
        return snap.points[idx][1]

    def route_key_to_node(self, key: str) -> Optional[str]:
        snap = self._snapshot
        if not snap.points:
            return None
        h = _hash(key)
        keys = [p[0] for p in snap.points]
        idx = bisect.bisect_right(keys, h) % len(keys)
        return snap.points[idx][1]

    def routing_table(self) -> dict[str, object]:
        snap = self._snapshot
        return {
            "nodes": list(snap.nodes),
            "advertise_urls": dict(snap.advertise_urls),
            "vnodes_per_node": self._vnodes,
            "ring_size": len(snap.points),
        }

    def emit_nginx_upstream(self, upstream_name: str = "helen_cluster") -> str:
        """Render an nginx ``upstream`` block listing currently-active nodes."""
        snap = self._snapshot
        lines = [f"upstream {upstream_name} {{", "    ip_hash;"]
        for nid, url in snap.advertise_urls.items():
            # strip scheme for nginx upstream
            host = url.replace("http://", "").replace("https://", "")
            lines.append(f"    server {host};  # node {nid[:8]}")
        lines.append("}")
        return "\n".join(lines)

    def emit_haproxy_backend(self, backend_name: str = "helen_cluster") -> str:
        snap = self._snapshot
        lines = [f"backend {backend_name}", "    balance source", "    hash-type consistent"]
        for nid, url in snap.advertise_urls.items():
            host = url.replace("http://", "").replace("https://", "")
            lines.append(f"    server n_{nid[:8]} {host} check")
        return "\n".join(lines)

    async def rebalance_on_node_change(self) -> RingSnapshot:
        """Recompute the ring from the current set of active nodes.
        Idempotent — safe to call frequently."""
        nodes = await self._registry.get_active_nodes()
        async with self._lock:
            entries: list[tuple[int, str]] = []
            urls: dict[str, str] = {}
            ids: list[str] = []
            for n in nodes:
                if n.status != "active":
                    continue
                ids.append(n.node_id)
                urls[n.node_id] = n.advertise_url
                for i in range(self._vnodes):
                    entries.append((_hash(f"{n.node_id}#{i}"), n.node_id))
            entries.sort(key=lambda e: e[0])
            snap = RingSnapshot(nodes=ids, advertise_urls=urls, points=entries)
            self._snapshot = snap
            return snap

    # ── internals ───────────────────────────────────────────

    async def _refresh_loop(self, interval: float) -> None:
        while not self._stop.is_set():
            try:
                await self.rebalance_on_node_change()
            except Exception as exc:                                # pragma: no cover
                logger.warning("sticky_router: refresh failed (%s)", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue


# ── singleton ───────────────────────────────────────────────


_singleton: Optional[StickyRouter] = None


def get_sticky_router() -> StickyRouter:
    global _singleton
    if _singleton is None:
        _singleton = StickyRouter()
    return _singleton
