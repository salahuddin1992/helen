"""
Kademlia-inspired DHT routing for the Helen federation.

Why
---
Up to ~100 servers the existing flood-then-cache approach is fine: each
``federated_emit.emit_to_user`` call fans out to every known peer, dedup
kills loops, and ``remember_origin`` collapses subsequent calls to O(1).

Past ~1,000 servers two things break down:

  * ``peer_registry`` grows unbounded — fine memory-wise (10k × ~500 B
    = 5 MB) but every flood tries every peer, so first-contact cost is
    O(N) HTTP POSTs.
  * gossip already shrinks discovery to O(N · √N), but that's still
    50k requests/sec at N=10,000 with the 20s default interval.

Kademlia gives O(log N) per lookup with a routing table bounded to
~K · 160 ≈ 3,200 entries regardless of the network's true size.

Design (deliberately minimal)
-----------------------------
This module implements the *routing-table half* of Kademlia, which is
what the chain-routing layer needs for closest-K forwarding. The full
iterative-lookup state machine + STORE/FIND_VALUE for arbitrary keys
is out of scope here: Helen's federation routes by ``server_id``
(64-char alphanumeric), not arbitrary content keys, and we already have
``federated_emit.remember_origin`` doing O(1) cache lookups for users
once a route is learned.

Distance metric
---------------
``XOR(left_id, right_id)`` over the SHA-256 prefix of each side's
``server_id``. SHA-256 because raw server_id alphabet is base62 not
base16 — hashing folds it into a uniform 256-bit integer suitable for
xor.

Public surface
--------------
* :class:`KademliaRoutingTable` — owns 160 k-buckets, each capped at K.
* :meth:`record_peer(peer)` — call on every peer contact (UDP ingest,
  gossip receipt, successful emit).
* :meth:`closest(target_id, k=K)` — return up to *k* peers closest to
  ``target_id`` by XOR distance. Used by the federation router to pick
  forwarding targets instead of fanning out to everyone.
* :func:`xor_distance(a, b)` — exposed for tests and metrics.
"""

from __future__ import annotations

import hashlib
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Iterable

from app.core.logging import get_logger

logger = get_logger(__name__)


# Standard Kademlia constants. K is the per-bucket capacity (Kademlia
# papers usually pick 20); 160 buckets correspond to the 160 bits of
# our SHA-256 prefix used for the distance metric.
K = 20
ID_BITS = 160


def _id_to_int(server_id: str) -> int:
    """Hash an arbitrary server_id into a 160-bit integer key.

    SHA-256 → take the first 20 bytes (160 bits). Same SHA-256 prefix
    is used everywhere so distance comparisons are consistent.
    """
    if not server_id:
        return 0
    digest = hashlib.sha256(server_id.encode("utf-8", errors="ignore")).digest()
    return int.from_bytes(digest[: ID_BITS // 8], "big")


def xor_distance(a: str, b: str) -> int:
    """XOR distance between two server_ids. Returns a 160-bit integer;
    smaller = closer. ``xor_distance(x, x) == 0``."""
    return _id_to_int(a) ^ _id_to_int(b)


def _bucket_index(self_id: int, peer_id: int) -> int:
    """Bucket #i contains peers whose XOR distance from us has its
    highest set bit at position i. So bucket 159 = farthest, bucket 0 =
    closest non-self peer.
    """
    dist = self_id ^ peer_id
    if dist == 0:
        # Same id as ourselves — should never happen because the caller
        # filters self before calling — but defend against it anyway.
        return 0
    return dist.bit_length() - 1


@dataclass
class _PeerRef:
    """Lightweight peer descriptor stored in k-buckets. We keep just
    the bits needed for routing decisions; the full PeerRecord lives
    in ``peer_registry`` and is fetched on demand."""
    server_id: str
    last_seen: float
    int_id: int = 0    # cached hash(server_id) for fast distance math


@dataclass
class _KBucket:
    """One k-bucket — at most ``K`` peers, ordered by recency
    (oldest at the head, newest at the tail). Standard Kademlia
    eviction policy: when full and a new peer arrives, ping the
    oldest and only displace it if it fails to respond. Helen runs
    on a LAN with low churn, so we approximate with simple LRU."""
    entries: deque = field(default_factory=lambda: deque(maxlen=K))


class KademliaRoutingTable:
    """160-bucket routing table keyed off our own ``server_id``.

    Public methods are safe to call from multiple asyncio tasks AND
    from blocking threads — internal state is guarded by a re-entrant
    Lock. Reads (closest, snapshot) take a brief lock; writes
    (record_peer) take a slightly longer one.
    """

    def __init__(self, self_id: str) -> None:
        self._self_id = self_id
        self._self_int = _id_to_int(self_id)
        self._buckets: list[_KBucket] = [_KBucket() for _ in range(ID_BITS)]
        self._lock = threading.RLock()
        # server_id → last_seen for fast presence checks; mirrors the
        # bucket entries to avoid scanning all 160 buckets on every
        # update.
        self._index: dict[str, _PeerRef] = {}

    # ── Mutation ──────────────────────────────────────────────
    def record_peer(self, server_id: str, last_seen: float) -> None:
        """Note that we've seen ``server_id`` at ``last_seen`` (epoch
        seconds). Idempotent — calling repeatedly just refreshes
        recency. Drops a no-op if ``server_id == self_id``."""
        if not server_id or server_id == self._self_id:
            return
        peer_int = _id_to_int(server_id)
        with self._lock:
            existing = self._index.get(server_id)
            if existing is not None:
                # Refresh recency, move to the end of its bucket.
                existing.last_seen = last_seen
                bucket = self._buckets[_bucket_index(self._self_int, peer_int)]
                # Remove + re-append — deque has no built-in move-to-end.
                try:
                    bucket.entries.remove(existing)
                except ValueError:
                    pass
                bucket.entries.append(existing)
                return
            ref = _PeerRef(server_id=server_id, last_seen=last_seen,
                           int_id=peer_int)
            bidx = _bucket_index(self._self_int, peer_int)
            bucket = self._buckets[bidx]
            if len(bucket.entries) >= K:
                # Bucket full — evict oldest. Vanilla Kademlia would
                # ping the oldest and only evict on failure; LAN churn
                # is low enough that simple LRU is acceptable and
                # cheaper.
                oldest = bucket.entries[0]
                self._index.pop(oldest.server_id, None)
            bucket.entries.append(ref)
            self._index[server_id] = ref

    def forget_peer(self, server_id: str) -> None:
        with self._lock:
            ref = self._index.pop(server_id, None)
            if ref is None:
                return
            bidx = _bucket_index(self._self_int, ref.int_id)
            bucket = self._buckets[bidx]
            try:
                bucket.entries.remove(ref)
            except ValueError:
                pass

    # ── Queries ────────────────────────────────────────────────
    def closest(self, target_id: str, k: int = K) -> list[str]:
        """Return up to ``k`` server_ids closest to ``target_id`` by
        XOR distance. Result is sorted ascending (closest first).

        At small k this is essentially the canonical Kademlia
        ``find_node(target)`` reply; the caller can then forward
        messages or run an iterative lookup.
        """
        if k <= 0:
            return []
        target_int = _id_to_int(target_id)
        # Walk every bucket — cheap because the table is bounded
        # (≤ K · ID_BITS = 3200 entries even at network sizes of 100k+).
        candidates: list[tuple[int, str]] = []
        with self._lock:
            for bucket in self._buckets:
                for ref in bucket.entries:
                    candidates.append((ref.int_id ^ target_int, ref.server_id))
        candidates.sort(key=lambda t: t[0])
        return [sid for _, sid in candidates[:k]]

    def size(self) -> int:
        with self._lock:
            return len(self._index)

    def snapshot(self) -> dict:
        """Diagnostic view of the routing table. Returns per-bucket
        counts + total. Used by the admin dashboard to expose DHT
        health (e.g. "we know 312 peers spread across 47 buckets")."""
        with self._lock:
            buckets = [len(b.entries) for b in self._buckets]
        return {
            "self_id": self._self_id,
            "total_peers": sum(buckets),
            "buckets_used": sum(1 for c in buckets if c > 0),
            "max_bucket_size": K,
            "id_bits": ID_BITS,
            # Compact per-bucket histogram: only buckets with content.
            "bucket_sizes": [
                {"bucket": i, "count": c}
                for i, c in enumerate(buckets) if c > 0
            ],
        }

    def all_peer_ids(self) -> list[str]:
        with self._lock:
            return list(self._index.keys())


# ── User location store (DHT STORE half) ──────────────────
#
# Each Helen server is responsible for ANNOUNCING the users it hosts
# locally to its K-closest neighbors in the DHT (by XOR distance over
# user_id). Other servers can then ASK those K-closest neighbors
# "where does user X live?" and get a direct answer — no flooding,
# no chain walks.
#
# Entries TTL out so a user's record is automatically forgotten
# after a quiet period; in-flight clients re-announce on heartbeat.


@dataclass
class _UserLocation:
    """Where (which server) hosts a given user_id, plus expiry."""
    user_id: str
    origin_server_id: str
    learned_at: float
    expires_at: float


class UserLocationStore:
    """Sparse DHT replica of the cluster's user-to-server mapping.

    Each server holds the entries it was asked to STORE (typically
    because its server_id is one of the K closest to the user_id).
    Reads are O(1); writes are size-capped to bound memory under
    abuse. ``cleanup_expired`` runs from a background task.
    """

    def __init__(self, max_entries: int = 200_000) -> None:
        self._lock = threading.RLock()
        self._users: dict[str, _UserLocation] = {}
        self._max = max_entries

    def store(self, user_id: str, origin_server_id: str,
              ttl_seconds: float = 120.0) -> None:
        if not user_id or not origin_server_id:
            return
        import time as _t
        now = _t.time()
        with self._lock:
            self._users[user_id] = _UserLocation(
                user_id=user_id,
                origin_server_id=origin_server_id,
                learned_at=now,
                expires_at=now + ttl_seconds,
            )
            if len(self._users) > self._max:
                # Evict the oldest entry (by learned_at). Cheap O(N)
                # but only fires under abuse — typical clusters stay
                # well below the cap.
                oldest = min(self._users.items(), key=lambda kv: kv[1].learned_at)
                self._users.pop(oldest[0], None)

    def lookup(self, user_id: str) -> str | None:
        """Return the owner server_id, or None if unknown / expired."""
        import time as _t
        with self._lock:
            entry = self._users.get(user_id)
            if entry is None:
                return None
            if entry.expires_at < _t.time():
                self._users.pop(user_id, None)
                return None
            return entry.origin_server_id

    def forget(self, user_id: str) -> None:
        with self._lock:
            self._users.pop(user_id, None)

    def cleanup_expired(self) -> int:
        import time as _t
        now = _t.time()
        with self._lock:
            stale = [uid for uid, e in self._users.items() if e.expires_at < now]
            for uid in stale:
                self._users.pop(uid, None)
        return len(stale)

    def size(self) -> int:
        with self._lock:
            return len(self._users)


# Singleton — populated by /api/federation/dht/store_user receivers and
# read by /api/federation/dht/find_user + the federated emit fast path.
user_location_store = UserLocationStore()


# Module-level singleton instantiated lazily in ``get_routing_table()``
# so we don't have to compute server_id at import time (the discovery
# service initializes it on first ``get_server_id()`` call).
_routing_table: KademliaRoutingTable | None = None
_init_lock = threading.Lock()


def get_routing_table() -> KademliaRoutingTable:
    """Return the process-wide routing table, creating it if needed."""
    global _routing_table
    if _routing_table is not None:
        return _routing_table
    with _init_lock:
        if _routing_table is not None:
            return _routing_table
        from app.services.discovery_service import get_server_id
        _routing_table = KademliaRoutingTable(self_id=get_server_id())
        logger.info("kademlia_routing_table_initialized",
                    self_id=_routing_table._self_id[:12])
    return _routing_table


def reset_for_tests() -> None:
    """Drop the singleton — only call from tests that need a fresh
    routing table tied to a re-initialised discovery service."""
    global _routing_table
    with _init_lock:
        _routing_table = None


# ── Persistence ────────────────────────────────────────────
#
# At 1M-node scale, the routing table takes a non-trivial amount of
# DHT traffic to populate from a cold cache (every new peer learn is a
# successful lookup or gossip exchange). Persisting the table to disk
# means a restarted server picks up where it left off instead of
# slowly re-walking the network.
#
# Format is a flat JSON list — small enough that `json.dump` is fine
# even at the 3,200-entry hard cap. We snapshot every 5 minutes from a
# background task and on graceful shutdown.

import json as _json
import time as _time
from pathlib import Path as _Path


def _routing_state_path() -> _Path:
    """Where the routing-table snapshot lives. Picks up
    ``COMMCLIENT_DATA_DIR`` if set so multi-instance test harnesses
    write to per-server directories rather than a shared file."""
    import os as _os
    base = (_os.environ.get("COMMCLIENT_DATA_DIR")
            or _os.path.join(_os.path.dirname(_os.path.dirname(
                _os.path.dirname(_os.path.abspath(__file__)))), "data"))
    p = _Path(base)
    p.mkdir(parents=True, exist_ok=True)
    return p / "kademlia_routing_table.json"


def save_routing_table_to_disk() -> int:
    """Serialize the routing table to disk. Returns count saved."""
    rt = get_routing_table()
    rows = []
    with rt._lock:
        for ref in rt._index.values():
            rows.append({"sid": ref.server_id, "ts": ref.last_seen})
    path = _routing_state_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(_json.dumps(rows), encoding="utf-8")
    tmp.replace(path)
    logger.info("kademlia_state_saved", count=len(rows), path=str(path))
    return len(rows)


def load_routing_table_from_disk() -> int:
    """Restore previously-persisted peer entries. Returns count loaded.
    Silently returns 0 if no snapshot exists (cold start). Stale
    entries (older than 24h) are skipped — peer churn would invalidate
    them anyway."""
    path = _routing_state_path()
    if not path.exists():
        return 0
    try:
        rows = _json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    if not isinstance(rows, list):
        return 0
    rt = get_routing_table()
    cutoff = _time.time() - 86400.0
    loaded = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        sid = row.get("sid")
        ts = float(row.get("ts") or 0)
        if not sid or ts < cutoff:
            continue
        rt.record_peer(sid, ts)
        loaded += 1
    logger.info("kademlia_state_loaded", count=loaded, path=str(path))
    return loaded
