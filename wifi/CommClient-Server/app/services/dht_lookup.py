"""
Kademlia iterative FIND_VALUE for user→server resolution.

Walks the DHT a few hops at a time toward the owner of a given
``user_id``, using the ``/api/federation/dht/find_user`` RPC. Returns
the owning ``server_id`` as soon as any peer responds with a hit.

Why iterative (vs flood)
------------------------
A flood asks every known peer "do you host this user?" — at 100k
servers that's 100k HTTP POSTs per first contact. Iterative lookup
asks the α=3 closest peers we know; their responses give us K=20
even-closer peers; we ask the α closest of those; etc. The walk
converges in O(log N) hops with O(α · log N) total messages —
**~50 messages** at N=100,000 instead of 100,000.

Bounded depth
-------------
``MAX_ITERATIONS = 12`` caps the walk to keep cost predictable even
when the DHT is partially populated. With α=3 and K=20 each iteration
prunes ~5 bits of distance, so 12 iterations cover ~60 bits — plenty
for any cluster smaller than ~10¹⁸ servers.

Concurrency
-----------
α (alpha) parallel queries per iteration. Set low (3) so a slow peer
doesn't stall the whole lookup; we wait for the FIRST winner among
the α queries to start the next iteration with the closer peers.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)


# All knobs are env-tunable so 1k / 10k / 1M deployments can pick a
# matching iteration budget without code changes:
#   * ALPHA — concurrent in-flight queries per "round". 3 keeps the
#     lookup responsive under one slow peer; raising helps with very
#     deep walks.
#   * K — Kademlia replication factor (also bucket size).
#   * MAX_ITERATIONS — log₂(N) + slack. 20 covers 1,048,576 servers;
#     bump to 24 if you ever hit the 16M+ regime.
#   * PER_QUERY_TIMEOUT_SEC — kill a single hop after this. The
#     streaming lookup keeps α other queries in flight, so a slow peer
#     shouldn't block the whole walk.
import os as _os_l

def _env_int(name: str, default: int) -> int:
    try:
        v = _os_l.environ.get(name, "")
        return max(1, int(v)) if v else default
    except ValueError:
        return default

def _env_float(name: str, default: float) -> float:
    try:
        v = _os_l.environ.get(name, "")
        return max(0.1, float(v)) if v else default
    except ValueError:
        return default

ALPHA = _env_int("HELEN_DHT_ALPHA", 3)
K = _env_int("HELEN_DHT_K", 20)
MAX_ITERATIONS = _env_int("HELEN_DHT_MAX_ITERATIONS", 20)
PER_QUERY_TIMEOUT_SEC = _env_float("HELEN_DHT_QUERY_TIMEOUT_SEC", 4.0)


async def iterative_find_user(user_id: str) -> str | None:
    """Resolve ``user_id`` → ``origin_server_id`` via *streaming*
    iterative DHT walk — α queries are kept continuously in flight,
    and we advance to the next candidate as soon as ANY in-flight
    response arrives. This avoids the round-by-round latency tax
    where one slow peer stalled the whole iteration.

    At N=1,000,000 servers the worst-case walk is log₂(N)≈20 hops;
    streaming brings actual wall-clock close to (depth × median RTT)
    instead of (depth × p99 RTT) like a strict round-based walk.

    Returns the owner's ``server_id`` on success, None on full failure
    (no peers, all queries timed out, DHT cold). On miss the caller
    falls back to the legacy closest-K flood.
    """
    from app.services.dht_kademlia import (
        get_routing_table, user_location_store, xor_distance,
    )
    from app.services.peer_registry import peer_registry
    from app.services.federation_service import federation_service

    # 1. Local STORE hit — no walk needed.
    local = user_location_store.lookup(user_id)
    if local:
        return local

    # 2. Seed the shortlist with the K closest peers we already know.
    rt = get_routing_table()
    seed_ids = rt.closest(user_id, k=K)
    if not seed_ids:
        logger.debug("dht_lookup_cold", user_id=user_id[:12])
        return None

    queried: set[str] = set()
    shortlist: list[str] = list(seed_ids)
    shortlist.sort(key=lambda sid: xor_distance(sid, user_id))

    t0 = time.monotonic()
    inflight: dict[asyncio.Task, str] = {}  # task → sid being queried
    iterations_used = 0

    def _next_candidate() -> str | None:
        """Pop the closest peer we haven't queried yet."""
        for sid in shortlist:
            if sid not in queried:
                return sid
        return None

    async def _probe(sid: str) -> dict | None:
        peer = await peer_registry.get(sid)
        if peer is None:
            return None
        return await federation_service.dht_find_user(
            peer, user_id=user_id, k=K,
        )

    def _spawn_up_to_alpha() -> None:
        """Top up in-flight queries until α tasks are running."""
        while len(inflight) < ALPHA:
            sid = _next_candidate()
            if sid is None:
                return
            queried.add(sid)
            t = asyncio.create_task(_probe(sid))
            inflight[t] = sid

    _spawn_up_to_alpha()

    while inflight and iterations_used < MAX_ITERATIONS * ALPHA:
        # Wait for the FIRST in-flight response — don't block on slow peers.
        done, _ = await asyncio.wait(
            list(inflight.keys()), return_when=asyncio.FIRST_COMPLETED,
        )
        for task in done:
            iterations_used += 1
            sid = inflight.pop(task)
            try:
                r = task.result()
            except Exception:
                r = None
            if not isinstance(r, dict):
                continue
            origin = r.get("origin")
            if origin:
                # Cancel the rest — we have our answer.
                for other in inflight.keys():
                    other.cancel()
                user_location_store.store(user_id, origin, ttl_seconds=120.0)
                logger.info(
                    "dht_lookup_hit", user_id=user_id[:12],
                    origin=origin[:12],
                    queries=iterations_used,
                    ms=int((time.monotonic() - t0) * 1000),
                )
                return origin
            # Merge any new candidates into the shortlist.
            for p in (r.get("peers") or []):
                psid = p.get("server_id")
                if not psid or psid in queried or psid in shortlist:
                    continue
                shortlist.append(psid)
                try:
                    rt.record_peer(psid, time.time())
                except Exception:
                    pass
            shortlist.sort(key=lambda x: xor_distance(x, user_id))
            shortlist = shortlist[:K]
        # Refill α in-flight queries with the newly-closer candidates.
        _spawn_up_to_alpha()

    # No hit anywhere along the walk.
    for task in inflight.keys():
        task.cancel()
    logger.debug("dht_lookup_miss", user_id=user_id[:12],
                 queries=iterations_used)
    return None


async def announce_user_to_dht(user_id: str, ttl_seconds: float = 120.0) -> int:
    """STORE this user's location on the K closest peers.

    Called from ``presence_service.connect`` and periodically from a
    re-announce loop so entries don't expire while the user is still
    online. Returns the number of peers that ACK'd the STORE.
    """
    from app.services.dht_kademlia import (
        get_routing_table, user_location_store,
    )
    from app.services.peer_registry import peer_registry
    from app.services.federation_service import federation_service
    from app.services.discovery_service import get_server_id

    my_id = get_server_id()
    user_location_store.store(user_id, my_id, ttl_seconds=ttl_seconds)
    rt = get_routing_table()
    closest = rt.closest(user_id, k=K)
    if not closest:
        return 0

    async def _store(sid: str) -> int:
        peer = await peer_registry.get(sid)
        if peer is None:
            return 0
        ok = await federation_service.dht_store_user(
            peer, user_id=user_id, origin_server_id=my_id,
            ttl_seconds=ttl_seconds,
        )
        return 1 if ok else 0

    results = await asyncio.gather(
        *(_store(sid) for sid in closest), return_exceptions=True,
    )
    return sum(1 for r in results if isinstance(r, int) and r > 0)
