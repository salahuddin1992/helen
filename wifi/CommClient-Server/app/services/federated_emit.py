"""
Unified socket-event delivery that spans multiple Helen servers.

`emit_to_user(target_user_id, event, payload)` delivers an event to the
right place regardless of whether the user is connected locally or on a
sibling server:

  1. If the user has live sockets here, emit directly via Socket.IO.
  2. Else, if federation is on and we know which peer hosts them, POST
     to that peer's `/api/federation/emit`.
  3. Else, fan out the POST to every live peer — the one that owns the
     user accepts it, the rest ACK 202 with `delivered: 0`.

The `user_id → origin_server_id` mapping is a best-effort cache learned
from federated share_code lookups and successful federated emits. A
stale entry just costs one extra round-trip before fallback kicks in.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from app.core.config import get_settings
from app.core.logging import get_logger
from app.services import federation_resilience as _resilience

logger = get_logger(__name__)

# Legacy in-process cache — kept for any synchronous callers that imported
# `_origin_cache` directly. New code MUST go through the async resilience
# module which transparently uses Redis (cluster-shared) when configured
# and falls back to L1-only otherwise.
_origin_cache: dict[str, tuple[str, float]] = {}
_ORIGIN_TTL_SECONDS = 900.0  # 15 min


def remember_origin(user_id: str, origin_server_id: str) -> None:
    """Sync shim — schedules the async write into the resilience layer.

    Existing callers (`federation_service`, peers ingest path) call this
    synchronously from inside async handlers, so we fire-and-forget. The
    legacy `_origin_cache` is updated immediately for callers that read
    from it directly without awaiting.
    """
    if not user_id or not origin_server_id:
        return
    _origin_cache[user_id] = (origin_server_id, time.time() + _ORIGIN_TTL_SECONDS)
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(_resilience.remember_origin(user_id, origin_server_id))
    except RuntimeError:
        # No running loop (e.g. unit test). The legacy dict update above is
        # still applied; Redis sync will happen on the next async caller.
        pass


def forget_origin(user_id: str) -> None:
    _origin_cache.pop(user_id, None)
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(_resilience.forget_origin(user_id))
    except RuntimeError:
        pass


async def _lookup_origin_async(user_id: str) -> str | None:
    """Resilience-aware lookup — checks L1 + Redis. Used by the async path
    in `emit_to_user` below. Falls back transparently to local-only if
    Redis is unavailable."""
    return await _resilience.lookup_origin(user_id)


def _lookup_origin(user_id: str) -> str | None:
    """Legacy sync lookup — only sees the in-process L1. Kept for any
    non-async caller that may still import it."""
    entry = _origin_cache.get(user_id)
    if entry is None:
        return None
    origin, expires = entry
    if expires < time.time():
        _origin_cache.pop(user_id, None)
        return None
    return origin


async def emit_to_user(
    target_user_id: str,
    event: str,
    payload: dict[str, Any],
) -> int:
    """Deliver `event` to `target_user_id` wherever they live.

    Returns the number of socket deliveries confirmed (best-effort). 0 means
    the user is unreachable (offline on this server, and no peer accepted
    the forward).
    """
    # 1. Local delivery
    from app.services.presence_service import presence_service
    from app.socket.server import sio

    sids = await presence_service.get_socket_ids(target_user_id)
    if sids:
        delivered = 0
        for sid in sids:
            try:
                await sio.emit(event, payload, to=sid)
                delivered += 1
            except Exception as e:
                logger.warning("local_emit_fail", sid=sid, error=str(e))
        return delivered

    # 2. Federation fallback
    settings = get_settings()
    if not settings.FEDERATION_ENABLED or not settings.FEDERATION_SECRET:
        return 0

    from app.services.federation_service import federation_service
    from app.services.peer_registry import peer_registry
    from app.services.federation_router import (
        next_message_id, resolve_max_hops, seen_cache,
    )

    # Fresh chain-routing envelope for this emit. ``seen_cache`` records
    # our own message_id so any peer that loops the forward back to us
    # during a flood is detected and dropped immediately.
    extra = {
        "message_id": next_message_id(),
        "hop_count": 0,
        "max_hops": resolve_max_hops(),
    }
    seen_cache.seen_and_record(extra["message_id"])

    origin = await _lookup_origin_async(target_user_id)
    if origin:
        ok = await federation_service.emit_to_remote_user(
            origin, target_user_id, event, payload, extra=extra,
        )
        if ok:
            return 1
        # Cache miss; forget and fan out.
        forget_origin(target_user_id)

    # 2.5. DHT iterative lookup — ask the K-closest peers to user_id
    #      "do you know who hosts this user?" Iterates in O(log N)
    #      hops with α=3 parallel queries. Successful resolution
    #      means we send EXACTLY ONE forward instead of flooding
    #      every peer in the cluster — what makes 100k-server
    #      meshes survive emit storms.
    try:
        from app.services.dht_lookup import iterative_find_user
        dht_origin = await iterative_find_user(target_user_id)
    except Exception as _e:
        logger.debug("dht_lookup_exception",
                     user_id=target_user_id, error=str(_e))
        dht_origin = None
    if dht_origin:
        ok = await federation_service.emit_to_remote_user(
            dht_origin, target_user_id, event, payload, extra=extra,
        )
        if ok:
            remember_origin(target_user_id, dht_origin)
            return 1
        # DHT pointed at a dead/lying peer — drop the cached origin
        # and fall through to the legacy flood as a last resort.
        from app.services.dht_kademlia import user_location_store
        user_location_store.forget(target_user_id)

    # 3. Fan out to all live peers. Each peer transits the message further
    # if it doesn't host the target — the flood converges when some peer
    # in the chain has the user locally (see federation_router's dedup).
    peers = await peer_registry.list(include_stale=False)
    if not peers:
        return 0

    async def _try_peer(peer) -> tuple[str, int]:
        ok = await federation_service.emit_to_remote_user(
            peer.server_id, target_user_id, event, payload, extra=extra,
        )
        return peer.server_id, (1 if ok else 0)

    results = await asyncio.gather(
        *[_try_peer(p) for p in peers], return_exceptions=True,
    )
    for r in results:
        if isinstance(r, tuple) and r[1] > 0:
            remember_origin(target_user_id, r[0])
            return r[1]
    return 0
