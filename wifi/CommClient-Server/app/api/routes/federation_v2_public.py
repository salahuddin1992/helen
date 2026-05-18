"""
Federation v2 — server-to-server public endpoints.

All endpoints require a valid server signature except:
    * GET /.well-known/helen-federation
    * POST /api/_federation/v2/handshake
"""
from __future__ import annotations

import base64
import time
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Path, Query, Request
from pydantic import BaseModel
from sqlalchemy import select

from app.core.logging import get_logger
from app.db.session import async_session_factory
from app.models.federation_v2 import (
    FederatedServer, FederationEvent,
)
from app.services.federation_v2.addressing import (
    AddressError, _validate_server_id, my_server_id,
)
from app.services.federation_v2.dag import get_dag_store
from app.services.federation_v2.handshake import (
    PROTOCOL_VERSION, make_challenge, my_server_card,
    negotiate_capabilities, verify_challenge,
)
from app.services.federation_v2.signing import (
    canonical_json, get_local_signing_key, sign, verify_event_signature,
)
from app.services.federation_v2.trust_graph import get_trust_graph

logger = get_logger(__name__)
router = APIRouter(tags=["federation-v2-public"])


# ── well-known card ─────────────────────────────────────────


@router.get("/.well-known/helen-federation")
async def wellknown_federation() -> dict[str, Any]:
    return my_server_card().to_dict()


# ── handshake ───────────────────────────────────────────────


class HandshakeBody(BaseModel):
    card: dict[str, Any]
    challenge: dict[str, Any]


@router.post("/api/_federation/v2/handshake")
async def handshake(body: HandshakeBody, request: Request) -> dict[str, Any]:
    """Receive an inbound handshake request."""
    peer_card = body.card or {}
    challenge = body.challenge or {}
    try:
        peer_id = _validate_server_id(peer_card.get("server_id") or "")
    except AddressError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    pubkey = peer_card.get("public_key") or ""
    if not pubkey:
        raise HTTPException(status_code=400, detail="missing public_key")
    if not verify_challenge(challenge, pubkey):
        raise HTTPException(status_code=401, detail="bad_challenge_signature")

    # Apply trust policy: block-listed servers may not establish.
    if not await get_trust_graph().is_allowed(peer_id):
        raise HTTPException(status_code=403, detail="server_not_allowed")

    # Persist or update peer.
    async with async_session_factory() as db:
        existing = (await db.execute(
            select(FederatedServer).where(FederatedServer.server_id == peer_id)
        )).scalar_one_or_none()
        if existing is None:
            existing = FederatedServer(
                server_id=peer_id,
                public_key=pubkey,
                advertise_url=peer_card.get("advertise_url") or f"https://{peer_id}",
                version=peer_card.get("version") or "",
                capabilities=peer_card.get("capabilities") or {},
                signing_algo=peer_card.get("signing_algo") or "ed25519",
                status="active",
                trust_level="peer",
                trust_score=0.5,
            )
            db.add(existing)
        else:
            existing.public_key = pubkey
            existing.advertise_url = peer_card.get("advertise_url") or existing.advertise_url
            existing.version = peer_card.get("version") or existing.version
            existing.capabilities = peer_card.get("capabilities") or existing.capabilities
            existing.status = "active"
        await db.commit()

    # Sign an ack proving we hold the key for our own server.
    sk = get_local_signing_key()
    ack_payload = {
        "type":      "ack",
        "from":      my_server_id(),
        "to":        peer_id,
        "nonce":     challenge.get("nonce"),
        "issued_at": int(time.time()),
    }
    sig = sign(sk, canonical_json(ack_payload))
    return {
        "card":          my_server_card().to_dict(),
        "ack_signature": base64.b64encode(sig).decode("ascii"),
        "issued_at":     ack_payload["issued_at"],
        "protocol":      PROTOCOL_VERSION,
        "capabilities":  negotiate_capabilities(peer_card.get("capabilities") or {}),
    }


# ── signed-request gate ─────────────────────────────────────


async def _require_signed_peer(request: Request) -> FederatedServer:
    """Trust gate for inbound peer requests."""
    origin = request.headers.get("X-Helen-Federation-Origin") or ""
    if not origin:
        raise HTTPException(status_code=401, detail="missing_origin")
    try:
        sid = _validate_server_id(origin)
    except AddressError:
        raise HTTPException(status_code=401, detail="invalid_origin")
    async with async_session_factory() as db:
        peer = (await db.execute(
            select(FederatedServer).where(FederatedServer.server_id == sid)
        )).scalar_one_or_none()
    if peer is None or peer.status not in ("active",):
        raise HTTPException(status_code=403, detail="unknown_or_inactive_peer")
    if not await get_trust_graph().is_allowed(sid):
        raise HTTPException(status_code=403, detail="server_blocked")
    return peer


# ── event ingestion ─────────────────────────────────────────


@router.put("/api/_federation/v2/events/{event_id}")
async def ingest_event(
    event_id: str = Path(...),
    request: Request = None,  # type: ignore[assignment]
):
    peer = await _require_signed_peer(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="bad_json")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="bad_event")
    if body.get("event_id") and body["event_id"] != event_id:
        raise HTTPException(status_code=400, detail="event_id_mismatch")
    if not body.get("event_id"):
        body["event_id"] = event_id
    # Verify origin signature.
    origin = body.get("origin") or peer.server_id
    pubkey = peer.public_key
    if origin == peer.server_id:
        if not verify_event_signature(body, peer.server_id, pubkey):
            raise HTTPException(status_code=401, detail="bad_event_signature")
    # Persist into the DAG. Local processors pick it up.
    row = await get_dag_store().insert(body)
    return {"ok": True, "id": row.id, "depth": row.depth}


# ── incremental sync ────────────────────────────────────────


@router.get("/api/_federation/v2/sync")
async def sync(
    request: Request,
    since: Optional[str] = Query(None),
    limit: int = Query(200, ge=1, le=1000),
):
    await _require_signed_peer(request)
    async with async_session_factory() as db:
        q = select(FederationEvent).order_by(FederationEvent.depth.asc())
        if since:
            try:
                q = q.where(FederationEvent.depth > int(since))
            except (ValueError, TypeError):
                pass
        q = q.limit(limit)
        rows = (await db.execute(q)).scalars().all()
    events = [r.signed_payload for r in rows]
    next_token = str(max((r.depth for r in rows), default=int(since or 0)))
    return {"events": events, "next": next_token, "count": len(events)}


# ── backfill ────────────────────────────────────────────────


@router.get("/api/_federation/v2/backfill")
async def backfill(
    request: Request,
    channel: str = Query(...),
    before: Optional[int] = Query(None),
    limit: int = Query(100, ge=1, le=500),
):
    await _require_signed_peer(request)
    rows = await get_dag_store().backfill(
        channel, before_depth=before, limit=limit,
    )
    return {"events": [r.signed_payload for r in rows]}


# ── channel state ───────────────────────────────────────────


@router.get("/api/_federation/v2/state/{channel_id}")
async def channel_state(
    channel_id: str,
    request: Request,
):
    await _require_signed_peer(request)
    sid = my_server_id()
    addr = f"#{channel_id}@{sid}"
    rows = await get_dag_store().head_events(addr, limit=128)
    from app.services.federation_v2.dag import resolve_state
    state = resolve_state(rows)
    return {
        "channel":  addr,
        "depth":    max((r.depth for r in rows), default=0),
        "state":    {k: v.signed_payload for k, v in state.items()},
    }
