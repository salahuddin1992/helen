"""P2P peer registry — in-memory store of Peer objects.

Distinct from ``services.peer_registry`` (broadcast-payload store).
This registry is the *p2p-layer* view, populated by:

  * peer_discovery (local broadcasts + mDNS)
  * peer_gossip    (transitive learning)
  * peer_handshake (post-auth promotion)

It exposes filter/sort helpers used by peer_selection.
"""

from __future__ import annotations

import threading
from typing import Iterable, Optional

from app.p2p.peer_model import Peer, PeerRole
from app.p2p.p2p_exceptions import PeerNotFoundError


class P2PPeerRegistry:
    _singleton: "P2PPeerRegistry | None" = None

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._peers: dict[str, Peer] = {}

    @classmethod
    def instance(cls) -> "P2PPeerRegistry":
        if cls._singleton is None:
            cls._singleton = P2PPeerRegistry()
        return cls._singleton

    # ── CRUD ───────────────────────────────────────────────

    def upsert(self, peer: Peer) -> Peer:
        with self._lock:
            existing = self._peers.get(peer.peer_id)
            if existing is None:
                self._peers[peer.peer_id] = peer
                return peer
            existing.last_seen = max(existing.last_seen, peer.last_seen)
            existing.host = peer.host or existing.host
            if peer.port:
                existing.port = peer.port
            existing.cluster_id = peer.cluster_id or existing.cluster_id
            existing.capabilities.update(peer.capabilities or {})
            existing.roles |= peer.roles
            existing.bridge_subnets = list({
                *existing.bridge_subnets, *(peer.bridge_subnets or []),
            })
            existing.extra.update(peer.extra or {})
            return existing

    def remove(self, peer_id: str) -> bool:
        with self._lock:
            return self._peers.pop(peer_id, None) is not None

    def get(self, peer_id: str) -> Optional[Peer]:
        with self._lock:
            return self._peers.get(peer_id)

    def require(self, peer_id: str) -> Peer:
        p = self.get(peer_id)
        if p is None:
            raise PeerNotFoundError(peer_id)
        return p

    def all(self) -> list[Peer]:
        with self._lock:
            return list(self._peers.values())

    # ── Filters ────────────────────────────────────────────

    def by_role(self, role: PeerRole) -> list[Peer]:
        return [p for p in self.all() if p.role is role]

    def fresh(self, max_age_sec: float = 60.0) -> list[Peer]:
        return [p for p in self.all() if p.is_fresh(max_age_sec)]

    def routable(self) -> list[Peer]:
        return [p for p in self.all() if p.is_routable()]

    def bridges(self) -> list[Peer]:
        return [p for p in self.all() if p.is_bridge()]

    def quarantined(self) -> list[Peer]:
        return [p for p in self.all() if p.is_quarantined()]

    # ── Diagnostics ────────────────────────────────────────

    def count_by_role(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for p in self.all():
            out[p.role.value] = out.get(p.role.value, 0) + 1
        return out

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "count":         len(self._peers),
                "count_by_role": self.count_by_role(),
                "fresh":         len(self.fresh()),
                "bridges":       len(self.bridges()),
                "quarantined":   len(self.quarantined()),
            }


def get_p2p_registry() -> P2PPeerRegistry:
    return P2PPeerRegistry.instance()
