"""
Consistent hashing — sharding work across a dynamic peer set.

When a value (user_id, room_id, file chunk) needs to live on exactly
one Helen-Server in the cluster, naive ``hash(key) % N`` re-shards the
entire keyspace whenever a peer joins or leaves. With 1,000 keys and
10 peers, dropping one peer moves ~900 keys to new homes.

Consistent hashing fixes that: each peer is mapped to one or more
*virtual nodes* on a 2³²-position ring, each key hashes to a position,
and the value lives on the first peer clockwise. Adding or removing
a peer only displaces keys in its own arcs — typical movement is
``keys / N`` instead of ``keys × (N-1) / N``.

We also expose a ``replicas_for(key, k=3)`` helper that returns the
next K peers clockwise, which is the standard way to pick where to
store ``k`` replicas of a value (used by ``replication_manager``).

This module is pure data — the caller decides what's hashed (peers
typically by ``server_id`` and keys by user_id / room_id).
"""

from __future__ import annotations

import bisect
import hashlib
import threading
from typing import Iterable


VNODES_PER_PEER = 128


def _h(s: str) -> int:
    """32-bit hash position on the ring."""
    return int.from_bytes(
        hashlib.sha1(s.encode()).digest()[:4],
        byteorder="big",
    )


class ConsistentHashRing:
    """Mutable ring — peers can be added/removed at runtime."""

    def __init__(self, vnodes_per_peer: int = VNODES_PER_PEER) -> None:
        self._lock = threading.RLock()
        self._vnodes_per_peer = vnodes_per_peer
        self._positions: list[int] = []                 # sorted
        self._owners:    list[str] = []                 # parallel array
        self._peer_set:  set[str] = set()

    # ── Mutation ────────────────────────────────────────────

    def add(self, peer: str) -> None:
        with self._lock:
            if peer in self._peer_set:
                return
            for i in range(self._vnodes_per_peer):
                pos = _h(f"{peer}#{i}")
                idx = bisect.bisect_left(self._positions, pos)
                self._positions.insert(idx, pos)
                self._owners.insert(idx, peer)
            self._peer_set.add(peer)

    def remove(self, peer: str) -> None:
        with self._lock:
            if peer not in self._peer_set:
                return
            new_pos, new_own = [], []
            for p, o in zip(self._positions, self._owners):
                if o != peer:
                    new_pos.append(p)
                    new_own.append(o)
            self._positions = new_pos
            self._owners    = new_own
            self._peer_set.discard(peer)

    def set_peers(self, peers: Iterable[str]) -> None:
        """Replace the ring contents in one shot. Used when the peer
        list changes wholesale (e.g. after gossip catches up)."""
        with self._lock:
            current = set(self._peer_set)
            target = {str(p) for p in peers if p}
            for gone in current - target:
                self.remove(gone)
            for new in target - current:
                self.add(new)

    # ── Lookup ──────────────────────────────────────────────

    def owner_of(self, key: str) -> str | None:
        with self._lock:
            if not self._positions:
                return None
            pos = _h(key)
            idx = bisect.bisect_right(self._positions, pos)
            if idx == len(self._positions):
                idx = 0
            return self._owners[idx]

    def replicas_for(self, key: str, k: int = 3) -> list[str]:
        """First K *distinct* peers clockwise from key's position."""
        with self._lock:
            if not self._positions:
                return []
            pos = _h(key)
            n = len(self._positions)
            start = bisect.bisect_right(self._positions, pos)
            seen: list[str] = []
            i = 0
            while i < n and len(seen) < k:
                idx = (start + i) % n
                owner = self._owners[idx]
                if owner not in seen:
                    seen.append(owner)
                i += 1
            return seen

    # ── Diagnostics ─────────────────────────────────────────

    def peer_count(self) -> int:
        with self._lock:
            return len(self._peer_set)

    def vnode_count(self) -> int:
        with self._lock:
            return len(self._positions)

    def keyspace_share(self, sample_keys: int = 10_000) -> dict[str, float]:
        """Estimate the % of the keyspace each peer owns by sampling."""
        from random import choices
        import string
        with self._lock:
            if not self._owners:
                return {}
        counts: dict[str, int] = {}
        for _ in range(sample_keys):
            k = "".join(choices(string.ascii_lowercase + string.digits, k=16))
            o = self.owner_of(k) or ""
            counts[o] = counts.get(o, 0) + 1
        total = sum(counts.values()) or 1
        return {p: round(100.0 * c / total, 2) for p, c in counts.items()}


# ── Cluster-wide singleton tied to node_registry ────────────────


_singleton: ConsistentHashRing | None = None


def get_ring() -> ConsistentHashRing:
    global _singleton
    if _singleton is None:
        _singleton = ConsistentHashRing()
    return _singleton


def refresh_from_registry() -> int:
    """Rebuild the ring from the current node_registry. Call from
    the discovery/gossip loops whenever the peer set changes.
    Returns the new peer count.
    """
    try:
        from app.services.node_registry import get_registry
    except ImportError:
        return 0
    reg = get_registry()
    peers = [n.node_id for n in reg.nodes(include_dead=False)]
    ring = get_ring()
    ring.set_peers(peers)
    return ring.peer_count()
