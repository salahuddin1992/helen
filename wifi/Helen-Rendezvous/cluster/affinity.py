"""
SessionAffinity — peer_id -> owning instance_id mapping.

When a Helen-Server opens a tunnel WebSocket against instance A, the WebSocket
itself can only ever live on instance A — Python objects don't replicate across
processes. But an external client may land on any rendezvous instance because
the load balancer is L4 / L7 round-robin. So instance B has to:

    1. lookup_tunnel(peer_id)             — yes, the tunnel exists somewhere
    2. lookup_affinity(peer_id)           — instance A owns the WS
    3. forward the request frame to A via pub/sub
    4. await the response coming back through pub/sub

That's exactly what CrossInstanceRelay implements. SessionAffinity is the
mapping that powers steps 1-2.

Storage is the standard `register_signal` API under the `affinity:` namespace,
so it works on every backend (memory + Redis variants) without new methods.
"""

from __future__ import annotations

import time
from typing import Any, Optional

import structlog

from storage.backend import StorageBackend

logger = structlog.get_logger(__name__)


AFFINITY_NS = "affinity"


class SessionAffinity:
    """Lightweight wrapper around `register_signal` with TTL refresh helpers."""

    def __init__(
        self,
        backend: StorageBackend,
        *,
        ttl: int = 60,
        refresh_threshold: int = 30,
    ) -> None:
        self._backend = backend
        self._ttl = ttl
        self._refresh_threshold = refresh_threshold

    async def bind(
        self,
        peer_id: str,
        instance_id: str,
        extra: Optional[dict[str, Any]] = None,
    ) -> bool:
        payload = {
            "peer_id": peer_id,
            "instance_id": instance_id,
            "bound_at": time.time(),
            **(extra or {}),
        }
        ok = await self._backend.register_signal(
            f"{AFFINITY_NS}:{peer_id}",
            payload,
            self._ttl,
        )
        logger.info(
            "affinity_bound",
            peer_id=peer_id,
            instance_id=instance_id,
            ttl_sec=self._ttl,
        )
        return ok

    async def lookup(self, peer_id: str) -> Optional[dict[str, Any]]:
        return await self._backend.lookup_signal(f"{AFFINITY_NS}:{peer_id}")

    async def owner_of(self, peer_id: str) -> Optional[str]:
        entry = await self.lookup(peer_id)
        if entry is None:
            return None
        owner = entry.get("instance_id")
        return str(owner) if owner else None

    async def release(self, peer_id: str) -> bool:
        ok = await self._backend.delete_signal(f"{AFFINITY_NS}:{peer_id}")
        if ok:
            logger.info("affinity_released", peer_id=peer_id)
        return ok

    async def refresh(self, peer_id: str, instance_id: str) -> bool:
        """Re-write the binding to extend its TTL."""
        return await self.bind(peer_id, instance_id)
