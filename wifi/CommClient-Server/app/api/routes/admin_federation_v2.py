"""
Federation v2 — admin REST endpoints. Requires ``federation.admin``.
"""
from __future__ import annotations

import base64
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db
from app.core.logging import get_logger
from app.models.federation_v2 import (
    FederatedChannel, FederatedServer,
    FederationEvent, FederationTrustToken,
)
from app.services.federation_v2.addressing import (
    AddressError, _validate_server_id, my_server_id,
)
from app.services.federation_v2.handshake import begin_handshake
from app.services.federation_v2.replication import (
    share_channel, unshare_channel,
)
from app.services.federation_v2.signing import (
    canonical_json, get_local_signing_key, sign,
)
from app.services.federation_v2.trust_graph import get_trust_graph
from app.services.rbac.enforcer import require_permission

logger = get_logger(__name__)
router = APIRouter(prefix="/api/admin/federation/v2", tags=["admin-federation-v2"])
_PERM = "federation.admin"


# ── shapes ──────────────────────────────────────────────────


class ServerOut(BaseModel):
    id: str
    server_id: str
    advertise_url: str
    status: str
    trust_level: str
    trust_score: float
    version: str
    signing_algo: str
    capabilities: dict[str, Any]
    last_seen: Optional[datetime]


class TrustIn(BaseModel):
    server_id: str
    trust_level: str = "peer"
    scope: str = "peer"
    expires_in_days: int = 365


class HandshakeIn(BaseModel):
    domain: str


class ShareChannelIn(BaseModel):
    with_server: str
    policy: str = "public"


# ── server listing ──────────────────────────────────────────


@router.get("/servers", response_model=list[ServerOut])
async def list_servers(
    db: AsyncSession = Depends(get_db),
    _u: str = Depends(require_permission(_PERM)),
):
    rows = (await db.execute(
        select(FederatedServer).order_by(desc(FederatedServer.last_seen))
    )).scalars().all()
    return [
        ServerOut(
            id=r.id, server_id=r.server_id, advertise_url=r.advertise_url,
            status=r.status, trust_level=r.trust_level, trust_score=r.trust_score,
            version=r.version, signing_algo=r.signing_algo,
            capabilities=r.capabilities or {}, last_seen=r.last_seen,
        )
        for r in rows
    ]


@router.post("/servers/handshake")
async def trigger_handshake(
    payload: HandshakeIn,
    _u: str = Depends(require_permission(_PERM)),
):
    server = await begin_handshake(payload.domain)
    if server is None:
        raise HTTPException(status_code=502, detail="handshake_failed")
    return {
        "ok": True,
        "server_id": server.server_id,
        "status": server.status,
    }


@router.post("/servers/trust")
async def add_trust(
    payload: TrustIn,
    db: AsyncSession = Depends(get_db),
    _u: str = Depends(require_permission(_PERM)),
):
    try:
        sid = _validate_server_id(payload.server_id)
    except AddressError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    row = (await db.execute(
        select(FederatedServer).where(FederatedServer.server_id == sid)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="unknown_server")
    if payload.trust_level not in ("trusted", "peer", "restricted", "untrusted"):
        raise HTTPException(status_code=400, detail="invalid_trust_level")
    row.trust_level = payload.trust_level
    # Sign a token making this attestation tamper-evident.
    sk = get_local_signing_key()
    token_body = {
        "type":           "trust",
        "issuer":         my_server_id(),
        "subject":        sid,
        "scope":          payload.scope,
        "issued_at":      int(time.time()),
        "expires_at":     int(time.time()) + max(1, payload.expires_in_days) * 86400,
        "trust_level":    payload.trust_level,
    }
    signature = sign(sk, canonical_json(token_body))
    signed = base64.b64encode(canonical_json({
        **token_body, "signature": base64.b64encode(signature).decode("ascii"),
    })).decode("ascii")
    tok = FederationTrustToken(
        issuing_server=my_server_id(),
        subject_server=sid,
        signed_token=signed,
        scope=payload.scope,
        issued_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(days=max(1, payload.expires_in_days)),
    )
    db.add(tok)
    await db.commit()
    await get_trust_graph().reload()
    return {"ok": True, "trust_level": row.trust_level}


@router.delete("/servers/{id}")
async def defederate_server(
    id: str = Path(...),
    db: AsyncSession = Depends(get_db),
    _u: str = Depends(require_permission(_PERM)),
):
    row = (await db.execute(
        select(FederatedServer).where(FederatedServer.id == id)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="not_found")
    sid = row.server_id
    await db.delete(row)
    await db.commit()
    get_trust_graph().add_to_blocklist(sid)
    return {"ok": True}


@router.post("/servers/{id}/suspend")
async def suspend_server(
    id: str = Path(...),
    db: AsyncSession = Depends(get_db),
    _u: str = Depends(require_permission(_PERM)),
):
    row = (await db.execute(
        select(FederatedServer).where(FederatedServer.id == id)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="not_found")
    row.status = "suspended"
    await db.commit()
    return {"ok": True, "status": row.status}


# ── events log ──────────────────────────────────────────────


@router.get("/events")
async def list_events(
    db: AsyncSession = Depends(get_db),
    _u: str = Depends(require_permission(_PERM)),
    limit: int = Query(200, ge=1, le=1000),
    kind: Optional[str] = None,
):
    q = select(FederationEvent).order_by(desc(FederationEvent.created_at))
    if kind:
        q = q.where(FederationEvent.kind == kind)
    q = q.limit(limit)
    rows = (await db.execute(q)).scalars().all()
    return [
        {
            "id":              r.id,
            "kind":            r.kind,
            "origin_server":   r.origin_server,
            "origin_event_id": r.origin_event_id,
            "channel":         r.channel_address,
            "sender":          r.sender_address,
            "depth":           r.depth,
            "processed":       r.processed,
            "rejected":        r.rejected,
            "rejection_reason": r.rejection_reason,
            "created_at":      r.created_at,
        }
        for r in rows
    ]


# ── trust graph ─────────────────────────────────────────────


@router.get("/trust-graph")
async def get_graph(
    _u: str = Depends(require_permission(_PERM)),
):
    return await get_trust_graph().export_graph()


# ── channel sharing ─────────────────────────────────────────


@router.post("/channels/{channel_id}/share")
async def share(
    channel_id: str,
    payload: ShareChannelIn,
    _u: str = Depends(require_permission(_PERM)),
):
    try:
        target = _validate_server_id(payload.with_server)
    except AddressError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    row = await share_channel(channel_id, target, policy=payload.policy)
    return {
        "ok": True,
        "federation_address": row.federation_address,
        "shared_with": row.shared_with,
    }


@router.delete("/channels/{channel_id}/share/{server_id}")
async def unshare(
    channel_id: str,
    server_id: str,
    _u: str = Depends(require_permission(_PERM)),
):
    try:
        target = _validate_server_id(server_id)
    except AddressError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await unshare_channel(channel_id, target)
    return {"ok": True}


@router.get("/channels")
async def list_channels(
    db: AsyncSession = Depends(get_db),
    _u: str = Depends(require_permission(_PERM)),
):
    rows = (await db.execute(
        select(FederatedChannel).order_by(desc(FederatedChannel.created_at))
    )).scalars().all()
    return [
        {
            "id":                 r.id,
            "channel_id":         r.channel_id,
            "federation_address": r.federation_address,
            "origin_server":      r.origin_server,
            "shared_with":        r.shared_with or [],
            "policy":             r.policy,
            "state_version":      r.state_version,
        }
        for r in rows
    ]
