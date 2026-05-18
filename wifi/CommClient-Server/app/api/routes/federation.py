"""
Internal federation endpoints — called by sibling Helen servers, NOT by
end-user clients. Every route is gated by HMAC signature verification
(shared FEDERATION_SECRET).

Routes
------
GET  /api/federation/users/by-code/{code}
     Look up a user's public profile if they're hosted on this server.
     404 if not local.

POST /api/federation/emit
     { target_user_id, event, payload } — re-emit the event over Socket.IO
     to that user's sockets, if they're currently connected here.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.federation_auth import (
    HEADER_ORIGIN,
    HEADER_SIGNATURE,
    HEADER_TIMESTAMP,
    verify_request,
)
from app.core.logging import get_logger
from app.core.share_code import is_valid_share_code
from app.db.session import async_session_factory
from app.models.user import User

logger = get_logger(__name__)
settings = get_settings()
router = APIRouter(prefix="/federation", tags=["federation"])


async def _verify(request: Request) -> bytes:
    """Read the body and verify the HMAC. Raises 401/403 on failure.

    Returns the raw body bytes so handlers can parse it themselves without
    re-reading (Starlette lets you call .body() multiple times but only if
    cached — we cache it here).

    Peer-approval gate
    ------------------
    HMAC-valid does NOT imply approved. A peer that's been discovered
    and HMAC-verified but is still in WAITING/PENDING/AWAITING (or a
    peer that was DENIED) must NOT be able to hit our federation
    endpoints. Once we've authenticated the HMAC, we look up the
    sender (X-Federation-Origin) in ``server_nodes`` and refuse if
    it isn't in ACTIVE_PEER_STATES.

    Exempted endpoints
    ------------------
    The peer-announce/probe endpoints are excluded — that's literally
    how a peer GETS approved, so gating them creates a deadlock. Only
    the lifecycle/emit endpoints carry this gate.
    """
    if not settings.FEDERATION_ENABLED:
        raise HTTPException(status_code=403, detail="federation disabled")
    body = await request.body()
    # Path here is the raw URL path without host. We match the signer's
    # `path` which includes /api prefix.
    ok, reason = verify_request(
        method=request.method,
        path=request.url.path,
        body=body,
        timestamp_header=request.headers.get(HEADER_TIMESTAMP),
        signature_header=request.headers.get(HEADER_SIGNATURE),
    )
    if not ok:
        logger.warning("federation_auth_rejected", reason=reason, path=request.url.path)
        raise HTTPException(status_code=401, detail="unauthenticated")

    # Peer-approval gate. Skip on the discovery/probe paths so a
    # newly-arriving peer can announce itself before being approved.
    sender = (request.headers.get("X-Federation-Origin") or "").strip()
    # Hard block: the operator-controlled sync_policy blocklist
    # rejects a peer for *every* federation endpoint, including the
    # discovery handshake — there is no way back in until the admin
    # unblocks. Checked before the approval gate so even unverified
    # arrivals from a blocked peer can't probe us.
    if sender:
        try:
            from app.services.sync_policy import get_sync_policy
            if get_sync_policy().is_blocked(sender):
                logger.warning(
                    "federation_blocked_by_sync_policy",
                    sender=sender[:24], path=request.url.path,
                )
                raise HTTPException(status_code=403, detail="peer_blocked")
        except HTTPException:
            raise
        except Exception:
            pass
    if sender:
        path = request.url.path or ""
        # Endpoints that are part of the discovery/approval handshake
        # itself — must remain reachable for peers in any state.
        EXEMPT_PREFIXES = (
            "/api/federation/peer-announce",
            "/api/federation/peer-probe",
            "/api/federation/dht/find_node",
            "/api/federation/gossip/peers",
            "/api/federation/presence/snapshot",
        )
        if not any(path.startswith(p) for p in EXEMPT_PREFIXES):
            try:
                from app.services.peer_approval_service import peer_approval_service
                from app.models.server_node import (
                    ACTIVE_PEER_STATES, TRANSIENT_PEER_STATES,
                )
                status_str = await peer_approval_service.get_peer_status(sender)
            except Exception as e:
                logger.warning(
                    "peer_gate_lookup_failed",
                    sender=sender[:24], path=path, error=str(e),
                )
                status_str = ""
            # Four cases:
            #   1. No row (status_str is None) → unknown peer.
            #      Fail-OPEN: HMAC alone gates. Legacy peers + tests.
            #   2. Row in ACTIVE_PEER_STATES → allow (steady-state).
            #   3. Row in TRANSIENT_PEER_STATES (DISCOVERED, AUTHENTICATING,
            #      VERIFIED, AUTO_ACCEPTED, APPROVED, PROVISIONING,
            #      SYNCING_STATE) → fail-OPEN. Same reasoning as case 1:
            #      HMAC + cluster match is enough security, and refusing
            #      mid-enrollment creates a chicken-and-egg with the
            #      first cross-server presence push from a peer whose
            #      own enrollment on this side is still racing through
            #      the state machine.
            #   4. Row in any other state (WAITING/PENDING/AWAITING/
            #      REJECTED/DENIED/EVICTED/empty-from-error) → block.
            blocking = (
                status_str is not None
                and status_str not in ACTIVE_PEER_STATES
                and status_str not in TRANSIENT_PEER_STATES
            )
            if blocking:
                logger.warning(
                    "federation_blocked_unapproved_peer",
                    sender=sender[:24], path=path, status=status_str,
                )
                raise HTTPException(
                    status_code=403,
                    detail="peer_not_approved",
                )
    return body


@router.get("/users/by-code/{code}")
async def federated_lookup_by_code(code: str, request: Request):
    await _verify(request)
    if not is_valid_share_code(code):
        raise HTTPException(status_code=400, detail="invalid code")
    async with async_session_factory() as db:  # type: AsyncSession
        result = await db.execute(select(User).where(User.share_code == code))
        user = result.scalar_one_or_none()
    if not user or not user.is_active:
        raise HTTPException(status_code=404, detail="not here")
    return {
        "user": {
            "id": user.id,
            "username": user.username,
            "share_code": user.share_code,
            "display_name": user.display_name,
            "avatar_url": user.avatar_url,
            "status": user.status,
        },
    }


@router.post("/dht/store_user", status_code=202)
async def federated_dht_store_user(request: Request):
    """Kademlia-style STORE — peer announces "I host this user_id" to
    the K servers closest to that user_id by XOR distance.

    Body: ``{"user_id": "...", "origin_server_id": "...", "ttl_seconds": 120}``
    """
    import json as _json
    body = await _verify(request)
    try:
        data = _json.loads(body.decode("utf-8") or "{}")
    except ValueError:
        raise HTTPException(status_code=400, detail="bad json")
    user_id = str(data.get("user_id") or "")
    origin = str(data.get("origin_server_id") or "")
    if not user_id or not origin:
        raise HTTPException(status_code=400, detail="missing fields")
    try:
        ttl = max(15.0, min(float(data.get("ttl_seconds") or 120.0), 3600.0))
    except (TypeError, ValueError):
        ttl = 120.0
    from app.services.dht_kademlia import user_location_store
    user_location_store.store(user_id, origin, ttl_seconds=ttl)
    from app.services import federation_metrics as _metrics
    _metrics.record_event(
        "dht_store_user",
        user_id=user_id[:12], origin=origin[:12],
        sender=request.headers.get("X-Federation-Origin", "")[:12],
    )
    return {"ok": True}


@router.post("/dht/find_user", status_code=200)
async def federated_dht_find_user(request: Request):
    """Kademlia-style FIND_VALUE for a user.

    If we hold a STORE entry for ``user_id``, return ``{"origin": "..."}``.
    Otherwise return the K closest peers we know to that user_id
    (by XOR distance) so the caller can iteratively walk closer.

    Body: ``{"user_id": "...", "k": 20}``
    """
    import json as _json
    body = await _verify(request)
    try:
        data = _json.loads(body.decode("utf-8") or "{}")
    except ValueError:
        raise HTTPException(status_code=400, detail="bad json")
    user_id = str(data.get("user_id") or "")
    if not user_id:
        raise HTTPException(status_code=400, detail="missing user_id")
    try:
        k = max(1, min(int(data.get("k") or 20), 50))
    except (TypeError, ValueError):
        k = 20

    from app.services.dht_kademlia import user_location_store, get_routing_table
    from app.services.peer_registry import peer_registry

    origin = user_location_store.lookup(user_id)
    if origin:
        # Hit — caller can stop walking and hand the message directly
        # to ``origin``. Include the peer record so caller has host:port.
        rec = await peer_registry.get(origin)
        peer_info = None
        if rec is not None:
            peer_info = {
                "server_id": rec.server_id,
                "name": rec.name,
                "host": rec.host,
                "port": rec.port,
            }
        return {"origin": origin, "owner_peer": peer_info, "peers": []}

    # Miss — return K closest peers by XOR distance to user_id so the
    # caller can iterate. Same contract as FIND_NODE.
    rt = get_routing_table()
    closest_ids = rt.closest(user_id, k=k)
    out = []
    for sid in closest_ids:
        rec = await peer_registry.get(sid)
        if rec is None:
            continue
        out.append({
            "server_id": rec.server_id,
            "name": rec.name,
            "host": rec.host,
            "port": rec.port,
        })
    return {"origin": None, "owner_peer": None, "peers": out}


@router.post("/dht/find_node", status_code=200)
async def federated_dht_find_node(request: Request):
    """Kademlia FIND_NODE RPC — replies with the K nearest peers we
    know to ``target_id`` by XOR distance.

    Body: ``{"target_id": "<server_id-hex>", "k": 20}``
    Returns: ``{"peers": [{"server_id": "...", "host": "...", "port": 0}, …]}``

    Used by the federation router to walk the network logarithmically
    instead of fanning out to every known peer. Authenticated with the
    same federation HMAC as every other /api/federation/* endpoint.
    """
    import json as _json
    body = await _verify(request)
    try:
        data = _json.loads(body.decode("utf-8") or "{}")
    except ValueError:
        raise HTTPException(status_code=400, detail="bad json")
    target_id = str(data.get("target_id") or "")
    if not target_id:
        raise HTTPException(status_code=400, detail="missing target_id")
    try:
        k = max(1, min(int(data.get("k") or 20), 50))
    except (TypeError, ValueError):
        k = 20

    from app.services.dht_kademlia import get_routing_table
    from app.services.peer_registry import peer_registry
    rt = get_routing_table()
    closest_ids = rt.closest(target_id, k=k)

    # Resolve each id back to its host/port via peer_registry so the
    # caller has enough info to make the next hop without another
    # discovery round-trip.
    out = []
    for sid in closest_ids:
        rec = await peer_registry.get(sid)
        if rec is None:
            continue
        out.append({
            "server_id": rec.server_id,
            "name": rec.name,
            "host": rec.host,
            "port": rec.port,
            "version": rec.version,
            "protocol": rec.protocol,
        })
    return {"peers": out, "asked_for": k, "table_size": rt.size()}


@router.post("/route/learned", status_code=202)
async def federated_route_learned(request: Request):
    """Chain-routing backpropagation: a downstream server tells us which
    peer actually hosts a given user. We cache that in the
    ``federated_emit`` origin map so the next emit to that user skips the
    flood and goes direct — dropping amplification from O(N) to O(1).

    Body: ``{"target_user_id": "...", "origin_server_id": "..."}``
    """
    import json as _json
    body = await _verify(request)
    try:
        data = _json.loads(body.decode("utf-8") or "{}")
    except ValueError:
        raise HTTPException(status_code=400, detail="bad json")
    target_user_id = str(data.get("target_user_id") or "")
    origin_server_id = str(data.get("origin_server_id") or "")
    if not target_user_id or not origin_server_id:
        raise HTTPException(status_code=400, detail="missing fields")
    from app.services.federated_emit import remember_origin
    remember_origin(target_user_id, origin_server_id)
    from app.services import federation_metrics as _metrics
    _metrics.record_event(
        "route_learned",
        target_user_id=target_user_id,
        origin=origin_server_id,
        sender=request.headers.get("X-Federation-Origin") or "",
    )
    return {"ok": True}


@router.post("/gossip/peers", status_code=202)
async def federated_gossip_peers(request: Request):
    """Peer-list exchange — any two federated servers share the peer
    lists they've learned, so discovery transcends the UDP broadcast
    domain. Receiving peer ingests the advertised peers it doesn't
    already know.

    Body: ``{"peers": [{"server_id": "...", "name": "...", "host": "...",
                        "port": 0, "version": "...", "protocol": "http"},
                       ...]}``

    Scales past the UDP broadcast ceiling: even in networks that drop
    255.255.255.255 (corporate, guest WiFi, multi-subnet), two servers
    that find each other via any mechanism (mDNS, active scan, manual
    seed) can teach each other about the rest of the mesh.
    """
    import json as _json
    body = await _verify(request)
    try:
        data = _json.loads(body.decode("utf-8") or "{}")
    except ValueError:
        raise HTTPException(status_code=400, detail="bad json")

    peers = data.get("peers") or []
    if not isinstance(peers, list):
        raise HTTPException(status_code=400, detail="peers must be a list")

    from app.services.peer_registry import peer_registry
    from app.services.discovery_service import get_server_id as _my_id
    from app.services import federation_metrics as _metrics

    my_id = _my_id()
    sender_peer = (
        request.headers.get("X-Federation-Origin")
        or (request.client.host if request.client else "")
    )
    ingested = 0
    for p in peers:
        if not isinstance(p, dict):
            continue
        if p.get("server_id") == my_id:
            continue  # don't re-learn ourselves
        # Reuse the same ingest path the UDP listener uses so every
        # advertised peer goes through the same TTL/dedup logic.
        payload = {
            "type": "commclient-server",
            **p,
        }
        rec = await peer_registry.ingest(payload, from_ip=str(p.get("host") or ""))
        if rec is not None:
            ingested += 1

    if sender_peer:
        _metrics.bump_peer(sender_peer, emits_received=1, bytes_in=len(body))
    _metrics.record_event(
        "gossip_received",
        sender=sender_peer,
        offered=len(peers),
        ingested=ingested,
    )
    return {"offered": len(peers), "ingested": ingested}


@router.post("/emit", status_code=202)
async def federated_emit(request: Request):
    """Re-emit an event over Socket.IO to a locally-connected user, with
    transit forwarding so chain topologies (A ↔ B ↔ C ↔ D) work end-to-end.

    Body: ``{ target_user_id, event, payload, message_id?, hop_count?,
              max_hops? }``

    Behavior:
      * If the body is missing ``message_id``, we mint one so downstream
        dedup still works.
      * If we've seen this ``message_id`` within the last 60s, drop —
        this is a duplicate that arrived via two different peers during
        the flood (loop protection).
      * If the target user has local sockets, deliver + cache the
        originating peer on our side so subsequent messages go direct.
      * Otherwise, if ``hop_count < max_hops``, forward to every known
        peer with ``hop_count + 1``. The first peer whose cache now
        contains the user answers direct on the return trip.
      * If ``hop_count >= max_hops`` and not local, drop with 202 so the
        upstream caller doesn't retry the chain indefinitely.

    Returns 202 regardless of actual delivery — peers treat the endpoint
    as fire-and-forget; they can't synchronously probe the end of a
    multi-hop chain anyway.
    """
    import json as _json

    body = await _verify(request)
    try:
        data = _json.loads(body.decode("utf-8") or "{}")
    except ValueError:
        raise HTTPException(status_code=400, detail="bad json")

    target_user_id = data.get("target_user_id")
    event = data.get("event")
    payload = data.get("payload") or {}
    if not target_user_id or not event:
        raise HTTPException(status_code=400, detail="missing target_user_id/event")

    from app.services.federation_router import (
        seen_cache, next_message_id, resolve_max_hops, forward_to_all_peers,
    )
    from app.services import federation_metrics as _metrics
    max_hops = int(data.get("max_hops") or resolve_max_hops())
    hop_count = int(data.get("hop_count") or 0)
    message_id = str(data.get("message_id") or "").strip() or next_message_id()

    # Best-effort: identify the peer that sent us this emit so per-peer
    # counters are meaningful. `X-Federation-Origin` is already defined in
    # core/federation_auth.py as a diagnostic header; every outbound signed
    # request sets it (federation_service side). Falls back to client IP.
    sender_peer = (
        request.headers.get("X-Federation-Origin")
        or (request.client.host if request.client else "")
    )
    body_len = len(body) if isinstance(body, (bytes, bytearray)) else 0

    # Loop prevention — drop if we've already seen this id within TTL.
    if seen_cache.seen_and_record(message_id):
        if sender_peer:
            _metrics.bump_peer(sender_peer,
                               emits_received=1, dedup_drops=1, bytes_in=body_len)
        _metrics.record_event(
            "dedup_drop",
            message_id=message_id,
            sender=sender_peer,
            hop_count=hop_count,
        )
        logger.debug("federation_emit_dup_drop",
                     message_id=message_id, hop_count=hop_count,
                     sender=sender_peer)
        return {"delivered": 0, "dedup": True}

    if sender_peer:
        _metrics.bump_peer(
            sender_peer,
            emits_received=1,
            forwards_incoming=(1 if hop_count > 0 else 0),
            bytes_in=body_len,
        )

    # Lazy import: the socket server isn't importable at module-load time
    # because it sits alongside the ASGI app and has its own bootstrap.
    from app.services.presence_service import presence_service
    from app.socket.server import sio

    # Seed / update the call-signal authz shadow BEFORE local fan-out.
    # When a remote user has just been told they're in a call (via this
    # forwarded event), their next outbound `signal:offer` lands here
    # too — and the signal-handler security check needs to know about
    # the call even though `call_service` doesn't track it on this
    # server. Cheap O(1) write; no-op for non-call events.
    #
    # We also record `sender_peer` as the call's origin so a callee on
    # this server can later forward accept/reject/leave RPCs back to
    # the owning server (see /api/federation/call/rpc).
    try:
        from app.services.call_signal_authz import apply_federation_event
        apply_federation_event(event, payload, origin_server_id=sender_peer or None)
    except Exception as _authz_e:
        logger.debug("call_signal_authz_seed_failed", error=str(_authz_e))

    sids = await presence_service.get_socket_ids(target_user_id)
    if sids:
        delivered = 0
        for sid in sids:
            try:
                await sio.emit(event, payload, to=sid)
                delivered += 1
            except Exception as e:
                logger.warning("federation_emit_fail", sid=sid, error=str(e))

        # Learned-route backprop: tell the sender "I host this user" so
        # the next message to them from upstream goes direct (O(1))
        # instead of flooding. We advertise our own server_id; the upstream
        # caches it via federated_emit.remember_origin. The ripple may
        # propagate further as each upstream hop repeats the same trick
        # when it gets the next message.
        if sender_peer and hop_count > 0:
            try:
                from app.services.discovery_service import get_server_id as _my_id
                from app.services.peer_registry import peer_registry
                from app.services.federation_service import federation_service
                sender_record = await peer_registry.get(sender_peer)
                if sender_record is not None:
                    # Fire-and-forget — don't block the delivery response.
                    import asyncio as _asyncio_bp
                    _asyncio_bp.create_task(
                        federation_service.route_learned_hint(
                            sender_record,
                            target_user_id=target_user_id,
                            origin_server_id=_my_id(),
                        )
                    )
            except Exception as _bp_e:
                logger.debug("route_learned_backprop_fail", error=str(_bp_e))

        _metrics.record_event(
            "delivered_local",
            target_user_id=target_user_id,
            event=event,
            message_id=message_id,
            hop_count=hop_count,
            sender=sender_peer,
            delivered=delivered,
        )
        # Tell any admin dashboards subscribed to the federation room
        # that a payload just landed here. Fire-and-forget.
        try:
            await sio.emit(
                "admin:federation_event",
                {
                    "kind": "delivered_local",
                    "target_user_id": target_user_id,
                    "event": event,
                    "message_id": message_id,
                    "hop_count": hop_count,
                    "sender": sender_peer,
                    "delivered": delivered,
                },
                room="admin_federation",
            )
        except Exception:
            pass
        logger.info("federation_emit_delivered_local",
                    target_user_id=target_user_id, event=event,
                    message_id=message_id, hop_count=hop_count,
                    sender=sender_peer,
                    delivered=delivered)
        return {"delivered": delivered, "hops": hop_count}

    # Not a local user — transit forward if we have budget left.
    if hop_count >= max_hops:
        logger.info("federation_emit_hop_limit",
                    target_user_id=target_user_id, hop_count=hop_count,
                    max_hops=max_hops, message_id=message_id)
        return {"delivered": 0, "dropped": "hop_limit"}

    # Wrap the forward so any per-peer transient failure (DNS, connection
    # refused mid-startup, dead stale peer) doesn't poison the 202 ACK
    # going back to the originating caller. The flood is best-effort; the
    # dedup cache ensures correctness even if some peers timeout and are
    # retried by a later hop.
    try:
        attempted = await forward_to_all_peers(
            target_user_id=target_user_id,
            event=event,
            payload=payload,
            message_id=message_id,
            hop_count=hop_count + 1,
        )
    except Exception as _e:  # pragma: no cover
        logger.warning("federation_forward_exception",
                       target_user_id=target_user_id,
                       message_id=message_id,
                       hop_count=hop_count,
                       error=str(_e))
        attempted = 0
    return {"delivered": 0, "forwarded_to": attempted, "hops": hop_count}


# ── Cross-server call lifecycle RPC ─────────────────────────
#
# When user A on server-1 calls user B on server-2, the ActiveCall
# entry lives on server-1 only. If B accepts on server-2, server-2's
# `v2_call_accept` handler doesn't find the call locally — so it
# forwards the action here via this RPC. We run the matching
# ``call_service`` operation locally and emit lifecycle events to
# every participant via ``emit_to_user`` (which itself hops federation
# as needed).


@router.post("/call/rpc", status_code=200)
async def federated_call_rpc(request: Request):
    """Run a call-lifecycle RPC on behalf of a sibling Helen server.

    Body: ``{ "rpc": "accept"|"reject"|"leave"|"hangup"|"reinvite",
              "call_id": "...", "user_id": "...", "extra": {...} }``

    The caller proves it's a peer via the federation HMAC. The
    ``user_id`` is trusted (federation is a closed-trust domain inside
    the Helen mesh; the per-user JWT auth happened on the originating
    server before the action was forwarded).

    Returns ``{"ok": true, "result": <handler-return>}`` on success or
    ``{"ok": false, "error": "..."}`` on failure (always 200 — the
    caller distinguishes via the body so transport-level retries don't
    misfire).
    """
    import json as _json
    body = await _verify(request)
    try:
        data = _json.loads(body.decode("utf-8") or "{}")
    except ValueError:
        raise HTTPException(status_code=400, detail="bad json")

    rpc = data.get("rpc")
    call_id = data.get("call_id")
    user_id = data.get("user_id")
    extra = data.get("extra") or {}
    if not rpc or not call_id or not user_id:
        raise HTTPException(status_code=400, detail="missing rpc/call_id/user_id")

    sender_peer = (
        request.headers.get("X-Federation-Origin")
        or (request.client.host if request.client else "")
    )
    logger.info("federated_call_rpc_received",
                rpc=rpc, call_id=call_id, user_id=user_id, sender=sender_peer)

    # Lazy imports — these reach into the call layer which depends on
    # services that aren't safely importable at module load (the socket
    # server boots after the FastAPI app graph is constructed).
    from app.services.call_service import call_service
    from app.services.call_signal_authz import call_signal_authz
    from app.services.presence_service import presence_service
    from app.socket.server import emit_to_user, sio

    call = call_service.get_call(call_id)
    if not call:
        return {"ok": False, "error": "call_not_found"}

    try:
        if rpc == "accept":
            # Honor the upstream idempotency_key so retries (caller
            # network blip mid-RPC, double-tap, etc.) collapse onto a
            # single accept rather than racing the call-state activation.
            from app.services.idempotency_cache import idempotency as _idem
            idempo_key = (extra.get("idempotency_key") or f"fed:{user_id}:accept")

            async def _do_origin_accept():
                up = await call_service.accept_call(call_id, user_id)
                _local_seed_after_change(call_id, up)
                await emit_to_user("call_accepted", {
                    "call_id": call_id,
                    "callee_id": user_id,
                }, up.initiator_id)
                return {
                    "status": "accepted",
                    "participants": list(up.participants.keys()),
                }

            result = await _idem.get_or_compute(call_id, idempo_key, _do_origin_accept)
            return {"ok": True, "result": result}

        if rpc == "reject":
            updated = await call_service.reject_call(call_id, user_id)
            await emit_to_user("call_rejected", {
                "call_id": call_id,
                "user_id": user_id,
            }, updated.initiator_id)
            call_signal_authz.clear(call_id)
            try:
                from app.db.session import async_session_factory as _sf
                async with _sf() as db:
                    await call_service.persist_call_log(db, updated)
            except Exception as _e:
                logger.warning("call_rpc_reject_persist_failed", error=str(_e))
            return {"ok": True, "result": {"status": "rejected"}}

        if rpc == "leave":
            pre_participants = list(call.participants.keys())
            updated = await call_service.leave_call(call_id, user_id)
            for pid in pre_participants:
                if pid == user_id:
                    continue
                await emit_to_user("call_participant_left", {
                    "call_id": call_id,
                    "user_id": user_id,
                }, pid)
            if updated.status == "ended":
                call_signal_authz.clear(call_id)
                try:
                    from app.db.session import async_session_factory as _sf
                    async with _sf() as db:
                        await call_service.persist_call_log(db, updated)
                except Exception as _e:
                    logger.warning("call_rpc_leave_persist_failed", error=str(_e))
            else:
                call_signal_authz.remove_participant(call_id, user_id)
            return {"ok": True, "result": {"status": "left"}}

        if rpc == "hangup":
            for pid in list(call.participants.keys()):
                if pid != user_id:
                    await emit_to_user("call_hangup", {
                        "call_id": call_id,
                        "ended_by": user_id,
                        "reason": "hangup",
                    }, pid)
            updated = await call_service.hangup(call_id, user_id)
            call_signal_authz.clear(call_id)
            try:
                from app.db.session import async_session_factory as _sf
                async with _sf() as db:
                    await call_service.persist_call_log(db, updated)
            except Exception as _e:
                logger.warning("call_rpc_hangup_persist_failed", error=str(_e))
            return {"ok": True, "result": {"status": "ended"}}

        if rpc == "join":
            # Cross-server join (BLOCKER-2 fix): a sibling server's user
            # is asking to join a group call whose ActiveCall lives here.
            # We run join_group_call locally, the resulting events
            # (call_participant_joined / call:active_call_started)
            # fan out via emit_to_user → federation back to the
            # caller's server. The local DB row keeps origin = us so
            # subsequent heartbeats / RPCs continue to land here.
            try:
                joined = await call_service.join_group_call(call_id, user_id)
                _local_seed_after_change(call_id, joined)
                return {
                    "ok": True,
                    "result": {
                        "status": "joined",
                        "call_id": call_id,
                        "participants": [
                            {"user_id": pid} for pid in joined.participants.keys()
                        ],
                        "routing": joined.routing,
                    },
                }
            except ValueError as _ve:
                # Already-in-call is treated as success so the caller's
                # client doesn't fall back to creating a parallel call.
                if "already" in str(_ve).lower():
                    return {"ok": True, "result": {"status": "already_in_call"}}
                return {"ok": False, "error": str(_ve)}

        if rpc == "heartbeat":
            # BLOCKER-1 fix: cross-server heartbeats land on the
            # origin so the orphan sweep (which scans last_heartbeat_at)
            # actually sees them. Origin uses local call_service to
            # touch in-memory state too.
            try:
                from app.services.call_state_persistence import (
                    call_state_persistence as _csp_hb,
                )
                await _csp_hb.heartbeat(call_id)
                return {"ok": True, "result": {"status": "heartbeat"}}
            except Exception as _e:
                return {"ok": False, "error": str(_e)}

        if rpc == "reinvite":
            target_id = extra.get("target_user_id")
            if not target_id:
                return {"ok": False, "error": "missing_target_user_id"}
            if call.initiator_id != user_id:
                return {"ok": False, "error": "forbidden_only_host"}
            payload = {
                "call_id":    call_id,
                "caller_id":  user_id,
                "media_type": call.media_type,
                "channel_id": call.channel_id,
                "is_reinvite": True,
            }
            delivered = await emit_to_user("call_incoming", payload, target_id)
            call_signal_authz.add_participant(call_id, target_id)
            return {"ok": True, "result": {"delivered_to_sockets": delivered}}

        return {"ok": False, "error": f"unknown_rpc:{rpc}"}

    except ValueError as ve:
        return {"ok": False, "error": str(ve)}
    except Exception as e:
        logger.error("federated_call_rpc_unhandled",
                     rpc=rpc, call_id=call_id, user_id=user_id, error=str(e))
        return {"ok": False, "error": "internal", "detail": str(e)}


def _local_seed_after_change(call_id: str, call) -> None:
    """Re-seed the authz shadow with the latest participant set on the
    origin server after a state-changing RPC. Origin = us; we are the
    server holding the authoritative ActiveCall."""
    try:
        from app.services.call_signal_authz import call_signal_authz
        from app.services.discovery_service import get_server_id
        call_signal_authz.seed(
            call_id,
            list(call.participants.keys()),
            origin_server_id=get_server_id(),
        )
    except Exception as e:
        logger.debug("local_seed_after_change_failed", error=str(e))


# ── Cross-server file proxy ─────────────────────────────────
#
# Multi-server file sharing stop-gap: a peer asks "do you host file X?"
# and either gets a 200 with the bytes (Range-aware) or a 404.
# Local file_service is the single source of truth; this endpoint just
# exposes that truth to authenticated peers.


@router.get("/files/{file_id}/locate", status_code=200)
async def federated_file_locate(file_id: str, request: Request):
    """Return ``{"local": true, "size": N, "mime": "..."}`` if this
    server hosts ``file_id`` locally, else 404.

    Used by sibling servers' download fallback: when their local DB
    doesn't have the file_id, they iterate peers calling this and
    proxy from whichever returns 200.
    """
    await _verify(request)
    from sqlalchemy import select as _sel
    from app.models.file import FileRecord
    from app.db.session import async_session_factory as _sf

    async with _sf() as db:
        rec = (await db.execute(
            _sel(FileRecord).where(FileRecord.id == file_id)
        )).scalar_one_or_none()
        if not rec:
            raise HTTPException(status_code=404, detail="not_local")
        import os as _os
        if not _os.path.exists(rec.storage_path):
            raise HTTPException(status_code=404, detail="storage_missing")
        try:
            size = _os.path.getsize(rec.storage_path)
        except OSError:
            raise HTTPException(status_code=404, detail="storage_missing")
        return {
            "local": True,
            "size": size,
            "mime": rec.mime_type,
            "original_name": rec.original_name,
            "checksum": rec.checksum_sha256,
        }


@router.get("/files/{file_id}/content")
async def federated_file_content(file_id: str, request: Request):
    """Stream the raw bytes of a locally-hosted file.

    Authorization (audit fix 2.4):
      The caller (relay server) MUST forward the requesting user's id
      via the ``X-Federation-Acting-User`` header. We re-verify
      ChannelService.is_member on OUR copy of the channel state before
      streaming. Without this, a member who was kicked on the owner
      server but not yet reflected on the relay could still pull bytes
      for several minutes during ChannelMember sync lag.
    """
    from fastapi.responses import StreamingResponse
    from sqlalchemy import select as _sel
    from app.models.file import FileRecord
    from app.db.session import async_session_factory as _sf
    from app.services.channel_service import ChannelService as _CS
    import os as _os

    await _verify(request)
    acting_user = request.headers.get("X-Federation-Acting-User") or ""
    if not acting_user or len(acting_user) > 64:
        raise HTTPException(status_code=400, detail="missing_acting_user")
    async with _sf() as db:
        rec = (await db.execute(
            _sel(FileRecord).where(FileRecord.id == file_id)
        )).scalar_one_or_none()
        if not rec or not _os.path.exists(rec.storage_path):
            raise HTTPException(status_code=404, detail="not_local")
        # Re-check authorization on the OWNER's authoritative state.
        # Three valid paths: uploader, channel member (group/dm), or
        # bare-file owner.
        if rec.uploader_id != acting_user:
            if rec.channel_id:
                if not await _CS.is_member(db, rec.channel_id, acting_user):
                    logger.warning(
                        "federated_file_proxy_unauthorized",
                        file_id=file_id,
                        acting_user=acting_user[:12],
                        channel_id=rec.channel_id[:12] if rec.channel_id else None,
                    )
                    raise HTTPException(status_code=403, detail="forbidden")
            else:
                # No channel — only uploader may read.
                logger.warning(
                    "federated_file_proxy_unauthorized_bare_file",
                    file_id=file_id, acting_user=acting_user[:12],
                )
                raise HTTPException(status_code=403, detail="forbidden")

    size = _os.path.getsize(rec.storage_path)
    rng = request.headers.get("range")
    start = 0
    end = size - 1
    status_code = 200
    if rng and rng.startswith("bytes="):
        try:
            spec = rng[len("bytes="):]
            s, _, e = spec.partition("-")
            start = int(s) if s else 0
            end = int(e) if e else size - 1
            end = min(end, size - 1)
            status_code = 206
        except Exception:
            start, end, status_code = 0, size - 1, 200
    length = end - start + 1

    async def _stream():
        chunk = 1 << 16  # 64 KiB
        async with __import__("aiofiles").open(rec.storage_path, "rb") as f:
            await f.seek(start)
            remaining = length
            while remaining > 0:
                buf = await f.read(min(chunk, remaining))
                if not buf:
                    break
                remaining -= len(buf)
                yield buf

    headers = {
        "Content-Length": str(length),
        "Content-Type":   rec.mime_type or "application/octet-stream",
        "Accept-Ranges":  "bytes",
    }
    if status_code == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return StreamingResponse(_stream(), status_code=status_code, headers=headers)


# ── Multi-hop UDP relay ─────────────────────────────────────
#
# These three endpoints let peers wire up a transparent forwarding chain
# across several Helen servers. The typical flow when server A wants a
# call to reach server D via B → C:
#
#   1. A asks C to allocate a relay whose next_hop is D:port_at_D.
#      C returns (C_host, C_port_in, relay_id_C).
#   2. A asks B to allocate a relay whose next_hop is (C_host, C_port_in).
#      B returns (B_host, B_port_in, relay_id_B).
#   3. A hands the client the ICE candidate (B_host, B_port_in).
#
# Client sends RTP to B_host:B_port_in → B pumps it to C_host:C_port_in
# → C pumps it to D:port_at_D. Return traffic walks the chain backwards
# using each hop's last-seen source.


@router.post("/relay/alloc", status_code=201)
async def federated_relay_alloc(request: Request):
    """Allocate a new UDP relay session on this server.

    Body:
      {
        "next_hop_host": "<ipv4>",
        "next_hop_port": <int>,
        "idle_ttl_seconds": <optional int, default 180>
      }

    Returns:
      {
        "relay_id": "...",
        "ingress_host": "<advertised LAN IP>",
        "ingress_port": <bound UDP port on this server>,
        "next_hop":    {"host": ..., "port": ...},
        "idle_ttl_seconds": ...
      }
    """
    import json as _json
    body = await _verify(request)
    try:
        data = _json.loads(body.decode("utf-8") or "{}")
    except ValueError:
        raise HTTPException(status_code=400, detail="bad json")

    next_hop_host = data.get("next_hop_host")
    next_hop_port = data.get("next_hop_port")
    idle_ttl = float(data.get("idle_ttl_seconds") or 180.0)
    if not next_hop_host or not isinstance(next_hop_port, int):
        raise HTTPException(
            status_code=400, detail="missing next_hop_host/next_hop_port",
        )
    if not (1 <= next_hop_port <= 65535):
        raise HTTPException(status_code=400, detail="bad port")

    from app.services.discovery_service import get_lan_ip
    from app.services.relay_worker import (
        RelayQuotaExceeded,
        relay_alloc_rate_limiter,
        relay_manager,
    )

    # Identify the peer making this request so quota/rate-limit accounting
    # sticks to them rather than to this server's advertised IP.
    peer_id = request.headers.get(HEADER_ORIGIN) or ""
    from app.services.federation_metrics import incr as _incr
    if not relay_alloc_rate_limiter.check(peer_id):
        _incr("relay_alloc_rate_limited")
        logger.warning("relay_alloc_rate_limited", peer=peer_id)
        raise HTTPException(
            status_code=429, detail="relay alloc rate limit exceeded",
        )
    try:
        session = await relay_manager.allocate(
            next_hop_host=next_hop_host,
            next_hop_port=next_hop_port,
            idle_ttl=idle_ttl,
            owner_peer=peer_id,
        )
    except RelayQuotaExceeded as e:
        _incr("relay_alloc_quota_denied")
        logger.warning("relay_alloc_quota_exceeded", peer=peer_id, reason=str(e))
        raise HTTPException(status_code=429, detail=str(e))
    _incr("relay_alloc_ok")
    advertised_host = get_lan_ip() or session.ingress_host
    logger.info(
        "federated_relay_alloc",
        relay_id=session.relay_id,
        advertised_host=advertised_host,
        ingress_port=session.ingress_port,
        next_hop=f"{next_hop_host}:{next_hop_port}",
    )
    return {
        "relay_id": session.relay_id,
        "ingress_host": advertised_host,
        "ingress_port": session.ingress_port,
        "next_hop": {"host": next_hop_host, "port": next_hop_port},
        "idle_ttl_seconds": idle_ttl,
    }


@router.post("/relay/release")
async def federated_relay_release(request: Request):
    """Tear down a previously allocated relay session.

    Body: { "relay_id": "..." }
    """
    import json as _json
    body = await _verify(request)
    try:
        data = _json.loads(body.decode("utf-8") or "{}")
    except ValueError:
        raise HTTPException(status_code=400, detail="bad json")

    rid = data.get("relay_id")
    if not rid:
        raise HTTPException(status_code=400, detail="missing relay_id")

    from app.services.relay_worker import relay_manager
    released = await relay_manager.release(rid)
    if released:
        from app.services.federation_metrics import incr as _incr
        _incr("relay_released")
    return {"released": released, "relay_id": rid}


@router.get("/relay/sessions")
async def federated_relay_list(request: Request):
    """Inventory of live relay sessions on this server. Peer-gated."""
    await _verify(request)
    from app.services.relay_worker import relay_manager
    return {"sessions": relay_manager.list_sessions()}


# ── Topology gossip ────────────────────────────────────────
#
# Returns the list of peers *this* server can see on its LAN. The path
# builder walks this graph, hop by hop, to find a route between two
# servers that aren't directly on the same broadcast domain.


# ── Presence ────────────────────────────────────────────────
#
# Push endpoint (POST /presence): peers fan out online/offline notices
# so every server builds its own directory of who's online across the
# federation. Body:
#   { kind: "online"|"offline", user_id, username, display_name,
#     origin_server_id }
#
# Pull endpoint (GET /presence/snapshot): returns the locally-known
# online users on this server only — the peer calls this periodically
# to rebuild its cache from ground truth in case it missed pushes.


@router.post("/presence", status_code=202)
async def federated_presence_push(request: Request):
    import json as _json
    body = await _verify(request)
    try:
        data = _json.loads(body.decode("utf-8") or "{}")
    except ValueError:
        raise HTTPException(status_code=400, detail="bad json")

    kind = data.get("kind")
    user_id = data.get("user_id")
    origin = data.get("origin_server_id")
    if not user_id or not origin or kind not in ("online", "offline"):
        raise HTTPException(status_code=400, detail="missing fields")

    from app.services.federated_presence import federated_presence
    if kind == "online":
        await federated_presence.upsert(
            user_id=user_id,
            username=data.get("username") or "",
            display_name=data.get("display_name") or "",
            origin_server_id=origin,
            status=data.get("status", "online"),
        )
    else:
        await federated_presence.remove(user_id)
    from app.services.federation_metrics import incr as _incr
    _incr("presence_pushes_received")
    return {"ok": True}


@router.get("/presence/snapshot")
async def federated_presence_snapshot(request: Request):
    """Local presence — who's online on *this* server, right now."""
    await _verify(request)
    from app.services.presence_service import presence_service
    from app.db.session import async_session_factory
    from app.models.user import User

    online_map = await presence_service.get_all_online()  # uid -> status
    if not online_map:
        return {"online": []}

    uids = list(online_map.keys())
    async with async_session_factory() as db:
        rows = (await db.execute(
            select(User).where(User.id.in_(uids))
        )).scalars().all()

    return {
        "online": [
            {
                "user_id": u.id,
                "username": u.username,
                "display_name": u.display_name,
                "status": online_map.get(u.id, "online"),
            }
            for u in rows if u.is_active
        ],
    }


@router.get("/presence/directory")
async def federated_presence_directory(request: Request):
    """Full cross-server online directory from this server's POV.

    Handy for clients that want a 'who's on the whole network' panel
    without running their own aggregation.
    """
    await _verify(request)
    from app.services.federated_presence import federated_presence
    return {"online": await federated_presence.list_online()}


@router.get("/peers")
async def federated_peers(request: Request):
    """Peers directly visible to this server (post-UDP-broadcast filter).

    Each entry includes the remote's `server_id`, `host`, and `port` so
    the caller can chain HTTP calls without a fresh discovery round.
    """
    await _verify(request)
    from app.services.discovery_service import get_server_id
    from app.services.peer_registry import peer_registry
    peers = await peer_registry.list(include_stale=False)
    return {
        "server_id": get_server_id(),
        "peers": [
            {
                "server_id": p.server_id,
                "name": p.name,
                "host": p.host,
                "port": p.port,
                "protocol": p.protocol,
            }
            for p in peers
        ],
    }
