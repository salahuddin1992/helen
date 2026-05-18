"""
Federation Health-Map admin REST + WebSocket API.

Mounted under ``/api/admin/federation`` with tag ``admin-federation``.
Every endpoint requires the caller's JWT to carry ``role: "admin"``;
destructive operations are audit-logged through ``app.core.audit``.

Design
------
* The router keeps zero business logic — every method is a thin
  ``Depends(require_role("admin"))`` wrapper around the federation_v2
  service layer (``services.federation_v2.*``).
* Graceful degradation: when an optional sub-service is missing (e.g.
  the consensus backend), the router surfaces ``enabled: false`` in
  the response rather than 500.
* All routes that mutate state push a frame onto the WebSocket bus.

This router complements ``admin_federation_v2.py`` — that one exposes
the *protocol* layer (servers / trust / channel-share / event DAG);
this one exposes the *operations* layer (health, shaper, certs,
quorum, diagnostics).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

import structlog
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Path,
    Query,
    Request,
    WebSocket,
    status,
)
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_log
from app.core.deps import get_db
from app.core.security_utils import require_role
from app.models.federation_event_log import FederationEventLog
from app.models.federation_peer import FederationPeerMeta
from app.models.federation_v2 import FederatedServer
from app.services.federation_v2.cert_manager import get_cert_manager
from app.services.federation_v2.diagnostics import get_diagnostics
from app.services.federation_v2.peer_manager import (
    METRICS_RETENTION_SEC,
    get_peer_manager,
)
from app.services.federation_v2.policy_engine import get_policy_engine
from app.services.federation_v2.quorum import get_quorum_manager
from app.services.federation_v2.replication_monitor import get_replication_monitor
from app.services.federation_v2.shaper import get_shaper
from app.services.federation_v2.ws_stream import get_ws_manager

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/admin/federation", tags=["admin-federation"])

require_admin = require_role("admin")


# ─────────────────────────────────────────────────────────────
# Pydantic shapes
# ─────────────────────────────────────────────────────────────


class PeerOut(BaseModel):
    id: str
    server_id: str
    hostname: str
    ip_address: str
    public_key: str
    advertise_url: str
    status: str
    trust_level: str
    trust_score: float
    version: str
    region: str
    role: str
    health_state: str
    quarantined: bool
    quarantined_reason: Optional[str] = None
    last_seen: Optional[datetime] = None
    last_handshake_at: Optional[datetime] = None
    last_rtt_ms: float = 0.0
    last_throughput_kbps: float = 0.0
    last_loss_pct: float = 0.0
    last_error_count: int = 0
    shaper_rule_id: Optional[str] = None
    cert_id: Optional[str] = None
    capabilities: dict[str, Any] = Field(default_factory=dict)


class ShaperIn(BaseModel):
    in_kbps: int = Field(ge=0)
    out_kbps: int = Field(ge=0)
    burst_kbps: int = Field(default=0, ge=0)
    priority: int = Field(default=4, ge=0, le=7)
    preset: str = "custom"
    params: dict[str, Any] = Field(default_factory=dict)


class RoleIn(BaseModel):
    role: str


class QuarantineIn(BaseModel):
    reason: str = ""


class PolicyIn(BaseModel):
    name: str
    description: str = ""
    priority: int = 100
    enabled: bool = True
    match: dict[str, Any] = Field(default_factory=dict)
    action: dict[str, Any] = Field(default_factory=dict)


class PolicySimulateIn(BaseModel):
    envelope: dict[str, Any]
    rules_override: Optional[list[dict[str, Any]]] = None


class ShaperBulkIn(BaseModel):
    preset: str
    params: dict[str, Any] = Field(default_factory=dict)


class DiagnoseIn(BaseModel):
    timeout_sec: float = 5.0


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────


def _client_ip(request: Optional[Request]) -> str:
    if request is None:
        return "unknown"
    try:
        return request.client.host if request.client else "unknown"
    except Exception:
        return "unknown"


async def _audit_and_broadcast(
    *,
    event: str,
    user_id: str,
    request: Optional[Request],
    details: Optional[dict[str, Any]] = None,
    broadcast_kind: Optional[str] = None,
    broadcast_payload: Optional[dict[str, Any]] = None,
    success: bool = True,
) -> None:
    audit_log(
        event=event,
        user_id=user_id,
        ip_address=_client_ip(request),
        success=success,
        details=details or {},
    )
    if broadcast_kind:
        try:
            await get_ws_manager().broadcast(
                broadcast_kind, broadcast_payload or {}
            )
        except Exception as exc:  # pragma: no cover - best effort
            logger.warning("fedmap_broadcast_failed", error=str(exc))


# ─────────────────────────────────────────────────────────────
# Peer listing + detail
# ─────────────────────────────────────────────────────────────


@router.get("/peers")
async def list_peers(
    user_id: str = Depends(require_admin),
) -> list[dict[str, Any]]:
    return await get_peer_manager().list_peers()


@router.get("/peers/{peer_id}")
async def get_peer(
    peer_id: str = Path(...),
    user_id: str = Depends(require_admin),
) -> dict[str, Any]:
    peer = await get_peer_manager().get_peer_detail(peer_id)
    if peer is None:
        raise HTTPException(status_code=404, detail="peer_not_found")
    return peer


@router.post("/peers/{peer_id}/handshake")
async def rehandshake(
    request: Request,
    peer_id: str = Path(...),
    user_id: str = Depends(require_admin),
) -> dict[str, Any]:
    result = await get_peer_manager().handshake(peer_id, actor=user_id)
    await _audit_and_broadcast(
        event="federation.handshake",
        user_id=user_id, request=request,
        details={"peer_id": peer_id, "ok": result.get("ok")},
        broadcast_kind="handshake",
        broadcast_payload={"peer_id": peer_id, **result},
        success=bool(result.get("ok")),
    )
    if not result.get("ok") and result.get("error") == "not_found":
        raise HTTPException(status_code=404, detail="peer_not_found")
    return result


# ─────────────────────────────────────────────────────────────
# Sync state
# ─────────────────────────────────────────────────────────────


@router.get("/peers/{peer_id}/sync-state")
async def get_sync_state(
    peer_id: str = Path(...),
    user_id: str = Depends(require_admin),
) -> dict[str, Any]:
    peer = await get_peer_manager().get_peer(peer_id)
    if peer is None:
        raise HTTPException(status_code=404, detail="peer_not_found")
    lag_map = await get_replication_monitor().lag_map()
    sid = peer["server_id"]
    per_table = {t: lag_map.get(t, {}).get(sid, {"lag_ms": 0, "samples": 0})
                 for t in lag_map}
    return {"server_id": sid, "tables": per_table}


@router.post("/peers/{peer_id}/sync-state")
async def force_peer_sync(
    request: Request,
    peer_id: str = Path(...),
    user_id: str = Depends(require_admin),
) -> dict[str, Any]:
    result = await get_replication_monitor().force_sync(peer_id)
    await _audit_and_broadcast(
        event="federation.force_sync",
        user_id=user_id, request=request,
        details={"peer_id": peer_id, "result": result},
        broadcast_kind="sync",
        broadcast_payload={"peer_id": peer_id, **result},
        success=bool(result.get("ok")),
    )
    if not result.get("ok") and result.get("error") == "not_found":
        raise HTTPException(status_code=404, detail="peer_not_found")
    return result


# ─────────────────────────────────────────────────────────────
# Metrics + bandwidth
# ─────────────────────────────────────────────────────────────


@router.get("/peers/{peer_id}/metrics")
async def peer_metrics(
    peer_id: str = Path(...),
    range_sec: int = Query(default=3600, ge=1, le=METRICS_RETENTION_SEC, alias="range"),
    user_id: str = Depends(require_admin),
) -> dict[str, Any]:
    peer = await get_peer_manager().get_peer(peer_id)
    if peer is None:
        raise HTTPException(status_code=404, detail="peer_not_found")
    history = get_peer_manager().metrics_history(peer["server_id"], range_sec=range_sec)
    return {
        "server_id": peer["server_id"],
        "range_sec": range_sec,
        "points":    [p.to_dict() for p in history],
        "summary":   {
            "rtt_ms":         peer["last_rtt_ms"],
            "throughput_kbps": peer["last_throughput_kbps"],
            "loss_pct":       peer["last_loss_pct"],
            "errors":         peer["last_error_count"],
        },
    }


@router.get("/peers/{peer_id}/bandwidth")
async def peer_bandwidth(
    peer_id: str = Path(...),
    user_id: str = Depends(require_admin),
) -> dict[str, Any]:
    peer = await get_peer_manager().get_peer(peer_id)
    if peer is None:
        raise HTTPException(status_code=404, detail="peer_not_found")
    sid = peer["server_id"]
    rule = await get_shaper().get_rule(sid)
    actuals = get_shaper().actuals(sid)
    return {
        "server_id":  sid,
        "configured": rule,
        "actual":     actuals,
    }


@router.put("/peers/{peer_id}/shaper")
async def set_peer_shaper(
    request: Request,
    payload: ShaperIn,
    peer_id: str = Path(...),
    user_id: str = Depends(require_admin),
) -> dict[str, Any]:
    peer = await get_peer_manager().get_peer(peer_id)
    if peer is None:
        raise HTTPException(status_code=404, detail="peer_not_found")
    try:
        rule = await get_shaper().set_rule(
            peer["server_id"],
            in_kbps=payload.in_kbps,
            out_kbps=payload.out_kbps,
            burst_kbps=payload.burst_kbps,
            priority=payload.priority,
            preset=payload.preset,
            params=payload.params,
            actor=user_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await _audit_and_broadcast(
        event="federation.shaper.set",
        user_id=user_id, request=request,
        details={"peer_id": peer_id, "rule": rule},
        broadcast_kind="shaper_change",
        broadcast_payload={"peer_id": peer_id, "rule": rule},
    )
    return rule


# ─────────────────────────────────────────────────────────────
# Cert
# ─────────────────────────────────────────────────────────────


@router.get("/peers/{peer_id}/cert")
async def peer_cert(
    peer_id: str = Path(...),
    user_id: str = Depends(require_admin),
) -> dict[str, Any]:
    info = await get_cert_manager().info(peer_id)
    if info is None:
        raise HTTPException(status_code=404, detail="peer_not_found")
    return info


@router.post("/peers/{peer_id}/cert/rotate")
async def rotate_cert(
    request: Request,
    peer_id: str = Path(...),
    user_id: str = Depends(require_admin),
) -> dict[str, Any]:
    result = await get_cert_manager().rotate(peer_id, actor=user_id)
    await _audit_and_broadcast(
        event="federation.cert.rotate",
        user_id=user_id, request=request,
        details={"peer_id": peer_id, "ok": result.get("ok")},
        broadcast_kind="cert",
        broadcast_payload={"peer_id": peer_id, **result},
        success=bool(result.get("ok")),
    )
    if not result.get("ok") and result.get("error") == "not_found":
        raise HTTPException(status_code=404, detail="peer_not_found")
    return result


# ─────────────────────────────────────────────────────────────
# Quarantine / role
# ─────────────────────────────────────────────────────────────


@router.post("/peers/{peer_id}/quarantine")
async def quarantine_peer(
    request: Request,
    payload: QuarantineIn,
    peer_id: str = Path(...),
    user_id: str = Depends(require_admin),
) -> dict[str, Any]:
    result = await get_peer_manager().quarantine(
        peer_id, reason=payload.reason, actor=user_id,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="peer_not_found")
    await _audit_and_broadcast(
        event="federation.peer.quarantine",
        user_id=user_id, request=request,
        details={"peer_id": peer_id, "reason": payload.reason},
        broadcast_kind="partition",
        broadcast_payload={"peer_id": peer_id, "quarantined": True, "reason": payload.reason},
    )
    return result


@router.delete("/peers/{peer_id}/quarantine")
async def release_peer(
    request: Request,
    peer_id: str = Path(...),
    user_id: str = Depends(require_admin),
) -> dict[str, Any]:
    result = await get_peer_manager().release(peer_id, actor=user_id)
    if result is None:
        raise HTTPException(status_code=404, detail="peer_not_found")
    await _audit_and_broadcast(
        event="federation.peer.release",
        user_id=user_id, request=request,
        details={"peer_id": peer_id},
        broadcast_kind="partition",
        broadcast_payload={"peer_id": peer_id, "quarantined": False},
    )
    return result


@router.post("/peers/{peer_id}/promote")
async def promote_peer(
    request: Request,
    payload: RoleIn,
    peer_id: str = Path(...),
    user_id: str = Depends(require_admin),
) -> dict[str, Any]:
    try:
        result = await get_peer_manager().promote(
            peer_id, role=payload.role, actor=user_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if result is None:
        raise HTTPException(status_code=404, detail="peer_not_found")
    await _audit_and_broadcast(
        event="federation.peer.promote",
        user_id=user_id, request=request,
        details={"peer_id": peer_id, "role": payload.role},
        broadcast_kind="role_change",
        broadcast_payload={"peer_id": peer_id, "role": payload.role, "kind": "promote"},
    )
    return result


@router.post("/peers/{peer_id}/demote")
async def demote_peer(
    request: Request,
    payload: RoleIn,
    peer_id: str = Path(...),
    user_id: str = Depends(require_admin),
) -> dict[str, Any]:
    try:
        result = await get_peer_manager().demote(
            peer_id, role=payload.role, actor=user_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if result is None:
        raise HTTPException(status_code=404, detail="peer_not_found")
    await _audit_and_broadcast(
        event="federation.peer.demote",
        user_id=user_id, request=request,
        details={"peer_id": peer_id, "role": payload.role},
        broadcast_kind="role_change",
        broadcast_payload={"peer_id": peer_id, "role": payload.role, "kind": "demote"},
    )
    return result


# ─────────────────────────────────────────────────────────────
# Audit / diagnose
# ─────────────────────────────────────────────────────────────


@router.get("/peers/{peer_id}/audit")
async def peer_audit(
    peer_id: str = Path(...),
    user_id: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict[str, Any]]:
    peer = await get_peer_manager().get_peer(peer_id)
    if peer is None:
        raise HTTPException(status_code=404, detail="peer_not_found")
    rows = (await db.execute(
        select(FederationEventLog)
        .where(FederationEventLog.server_id == peer["server_id"])
        .order_by(desc(FederationEventLog.occurred_at))
        .limit(limit)
    )).scalars().all()
    return [
        {
            "id":          r.id,
            "category":    r.category,
            "severity":    r.severity,
            "summary":     r.summary,
            "actor":       r.actor,
            "payload":     r.payload or {},
            "occurred_at": r.occurred_at.isoformat() if r.occurred_at else None,
            "success":     r.success,
        }
        for r in rows
    ]


@router.post("/peers/{peer_id}/diagnose")
async def diagnose_peer(
    request: Request,
    payload: Optional[DiagnoseIn] = None,
    peer_id: str = Path(...),
    user_id: str = Depends(require_admin),
) -> dict[str, Any]:
    payload = payload or DiagnoseIn()
    result = await get_diagnostics().diagnose(
        peer_id, timeout=payload.timeout_sec,
    )
    await _audit_and_broadcast(
        event="federation.diagnose",
        user_id=user_id, request=request,
        details={"peer_id": peer_id, "ok": result.get("ok")},
        broadcast_kind="diagnostic",
        broadcast_payload={"peer_id": peer_id, "summary": result.get("ok")},
        success=bool(result.get("ok")),
    )
    if result.get("error") == "not_found":
        raise HTTPException(status_code=404, detail="peer_not_found")
    return result


# ─────────────────────────────────────────────────────────────
# Topology + replication matrix
# ─────────────────────────────────────────────────────────────


@router.get("/topology")
async def topology(
    user_id: str = Depends(require_admin),
) -> dict[str, Any]:
    """Full federation mesh graph: nodes = peers, edges = trust links."""
    peers = await get_peer_manager().list_peers()
    nodes = [
        {
            "id":       p["server_id"],
            "label":    p["hostname"] or p["server_id"],
            "role":     p["role"],
            "region":   p["region"],
            "status":   p["status"],
            "health":   p["health_state"],
            "rtt_ms":   p["last_rtt_ms"],
            "trust":    p["trust_level"],
        }
        for p in peers
    ]
    # Edges — best-effort star from local server; trust_graph has the
    # canonical edges in federation_v2.
    local = "<local>"
    edges = [
        {
            "src":    local,
            "dst":    n["id"],
            "trust":  n["trust"],
            "status": n["status"],
        }
        for n in nodes
    ]
    return {"nodes": nodes, "edges": edges, "local": local, "count": len(nodes)}


@router.get("/replication/lag")
async def replication_lag(
    user_id: str = Depends(require_admin),
) -> dict[str, Any]:
    matrix = await get_replication_monitor().lag_map()
    conflicts = await get_replication_monitor().conflicts(limit=25)
    return {"matrix": matrix, "conflicts": conflicts}


# ─────────────────────────────────────────────────────────────
# Policies
# ─────────────────────────────────────────────────────────────


@router.get("/policies")
async def list_policies(
    user_id: str = Depends(require_admin),
) -> list[dict[str, Any]]:
    return await get_policy_engine().list_policies()


@router.post("/policies", status_code=201)
async def create_policy(
    request: Request,
    payload: PolicyIn,
    user_id: str = Depends(require_admin),
) -> dict[str, Any]:
    row = await get_policy_engine().create_policy(
        name=payload.name,
        description=payload.description,
        priority=payload.priority,
        enabled=payload.enabled,
        match=payload.match,
        action=payload.action,
        actor=user_id,
    )
    await _audit_and_broadcast(
        event="federation.policy.create",
        user_id=user_id, request=request,
        details={"policy_id": row["id"], "name": row["name"]},
        broadcast_kind="policy",
        broadcast_payload={"op": "create", "policy": row},
    )
    return row


@router.delete("/policies/{policy_id}")
async def delete_policy(
    request: Request,
    policy_id: str = Path(...),
    user_id: str = Depends(require_admin),
) -> dict[str, Any]:
    ok = await get_policy_engine().delete_policy(policy_id)
    if not ok:
        raise HTTPException(status_code=404, detail="policy_not_found")
    await _audit_and_broadcast(
        event="federation.policy.delete",
        user_id=user_id, request=request,
        details={"policy_id": policy_id},
        broadcast_kind="policy",
        broadcast_payload={"op": "delete", "policy_id": policy_id},
    )
    return {"ok": True}


@router.post("/policies/simulate")
async def simulate_policy(
    payload: PolicySimulateIn,
    user_id: str = Depends(require_admin),
) -> dict[str, Any]:
    decision = await get_policy_engine().simulate(
        payload.envelope, rules_override=payload.rules_override,
    )
    return decision.to_dict()


# ─────────────────────────────────────────────────────────────
# Quorum
# ─────────────────────────────────────────────────────────────


@router.get("/quorum")
async def quorum_state(
    user_id: str = Depends(require_admin),
) -> dict[str, Any]:
    state = await get_quorum_manager().state()
    members = await get_quorum_manager().members()
    split = await get_quorum_manager().split_brain()
    return {"state": state, "members": members, "split_brain": split}


@router.post("/quorum/election")
async def force_election(
    request: Request,
    user_id: str = Depends(require_admin),
) -> dict[str, Any]:
    result = await get_quorum_manager().force_election(actor=user_id)
    await _audit_and_broadcast(
        event="federation.quorum.election",
        user_id=user_id, request=request,
        details=result,
        broadcast_kind="quorum",
        broadcast_payload={"op": "election", **result},
        success=bool(result.get("ok")),
    )
    return result


# ─────────────────────────────────────────────────────────────
# Shaper bulk / global sync / cert rotate-all
# ─────────────────────────────────────────────────────────────


@router.post("/shaper/bulk")
async def shaper_bulk(
    request: Request,
    payload: ShaperBulkIn,
    user_id: str = Depends(require_admin),
) -> dict[str, Any]:
    try:
        rules = await get_shaper().apply_preset(
            payload.preset, payload.params, actor=user_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await _audit_and_broadcast(
        event="federation.shaper.bulk",
        user_id=user_id, request=request,
        details={"preset": payload.preset, "count": len(rules)},
        broadcast_kind="shaper_change",
        broadcast_payload={"op": "bulk", "preset": payload.preset, "count": len(rules)},
    )
    return {"ok": True, "preset": payload.preset, "rules": rules}


@router.post("/sync")
async def global_sync(
    request: Request,
    user_id: str = Depends(require_admin),
) -> dict[str, Any]:
    peers = await get_peer_manager().list_peers()
    results = []
    for p in peers:
        results.append(await get_replication_monitor().force_sync(p["server_id"]))
    await _audit_and_broadcast(
        event="federation.sync.global",
        user_id=user_id, request=request,
        details={"count": len(results)},
        broadcast_kind="sync",
        broadcast_payload={"op": "global", "count": len(results)},
    )
    return {"ok": True, "count": len(results), "results": results}


@router.post("/certs/rotate-all")
async def rotate_all_certs(
    request: Request,
    user_id: str = Depends(require_admin),
) -> dict[str, Any]:
    results = await get_cert_manager().rotate_all(
        reason="admin-bulk", actor=user_id,
    )
    await _audit_and_broadcast(
        event="federation.cert.rotate_all",
        user_id=user_id, request=request,
        details={"count": len(results)},
        broadcast_kind="cert",
        broadcast_payload={"op": "rotate_all", "count": len(results)},
    )
    return {"ok": True, "count": len(results), "results": results}


# ─────────────────────────────────────────────────────────────
# Diagnostics
# ─────────────────────────────────────────────────────────────


@router.post("/diagnostics/skew")
async def diagnostics_skew(
    request: Request,
    user_id: str = Depends(require_admin),
) -> dict[str, Any]:
    result = await get_diagnostics().time_skew()
    await _audit_and_broadcast(
        event="federation.diagnostics.skew",
        user_id=user_id, request=request,
        details={"warned": result.get("warned")},
        broadcast_kind="diagnostic",
        broadcast_payload={"op": "skew", "warned": result.get("warned")},
    )
    return result


class CertChainIn(BaseModel):
    peer_id: str


@router.post("/diagnostics/cert-chain")
async def diagnostics_cert_chain(
    request: Request,
    payload: CertChainIn,
    user_id: str = Depends(require_admin),
) -> dict[str, Any]:
    result = await get_diagnostics().cert_chain(payload.peer_id)
    await _audit_and_broadcast(
        event="federation.diagnostics.cert_chain",
        user_id=user_id, request=request,
        details={"peer_id": payload.peer_id, "ok": result.get("ok")},
        broadcast_kind="diagnostic",
        broadcast_payload={"op": "cert_chain", "peer_id": payload.peer_id, "ok": result.get("ok")},
        success=bool(result.get("ok")),
    )
    return result


# ─────────────────────────────────────────────────────────────
# WebSocket
# ─────────────────────────────────────────────────────────────


@router.websocket("/ws/federation")
async def federation_ws(ws: WebSocket) -> None:
    """Live federation event stream.

    Auth: ``?token=<jwt>`` query param. Must carry ``role: "admin"``.
    Frame schema is documented in ``services.federation_v2.ws_stream``.
    """
    await get_ws_manager().handle_connection(ws)
