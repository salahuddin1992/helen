"""
Phase 6 / Module AC — Cluster admin REST endpoints.

Mounted under ``/api/admin/cluster``. Requires the ``cluster.manage``
permission (except for the ``/pubsub/ingest`` ingestion hook which is
called server-to-server with a shared cluster_id check).
"""
from __future__ import annotations

import hmac
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Path, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.deps import get_db
from app.core.logging import get_logger
from app.models.cluster import ClusterNode, LeaderElection
from app.services.cluster.leader_election import get_cluster_leader
from app.services.cluster.node_registry import get_node_registry
from app.services.cluster.pubsub import get_pubsub
from app.services.cluster.session_store import get_session_store
from app.services.cluster.sticky_router import get_sticky_router
from app.services.rbac.enforcer import require_permission

logger = get_logger(__name__)

router = APIRouter(prefix="/api/admin/cluster", tags=["admin-cluster"])
_PERM = "cluster.manage"


# ── pydantic shapes ─────────────────────────────────────────


class NodeOut(BaseModel):
    id: str
    node_id: str
    hostname: str
    advertise_url: str
    status: str
    role: str
    version: str
    joined_at: datetime
    last_seen: datetime
    capabilities: dict[str, Any] = Field(default_factory=dict)


class LeaderOut(BaseModel):
    term: int
    leader_node_id: Optional[str]
    started_at: Optional[datetime]
    expires_at: Optional[datetime]
    is_self: bool
    self_node_id: str


class HealthOut(BaseModel):
    cluster_size: int
    active: int
    draining: int
    down: int
    leader: Optional[str]
    session_store: dict[str, Any]
    pubsub_node_id: str
    ring_size: int


# ── helpers ─────────────────────────────────────────────────


def _to_out(n: ClusterNode) -> NodeOut:
    return NodeOut(
        id=n.id, node_id=n.node_id, hostname=n.hostname,
        advertise_url=n.advertise_url, status=n.status,
        role=n.role, version=n.version, joined_at=n.joined_at,
        last_seen=n.last_seen, capabilities=n.capabilities or {},
    )


# ── routes ──────────────────────────────────────────────────


@router.get("/nodes", response_model=list[NodeOut])
async def list_nodes(
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM)),
):
    res = await db.execute(select(ClusterNode).order_by(ClusterNode.joined_at))
    return [_to_out(n) for n in res.scalars().all()]


@router.get("/leader", response_model=LeaderOut)
async def get_leader(
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM)),
):
    row = (await db.execute(select(LeaderElection))).scalar_one_or_none()
    elect = get_cluster_leader()
    if row is None:
        return LeaderOut(
            term=elect.current_term(),
            leader_node_id=None, started_at=None, expires_at=None,
            is_self=elect.is_leader(),
            self_node_id=elect.node_id,
        )
    return LeaderOut(
        term=int(row.term),
        leader_node_id=row.leader_node_id,
        started_at=row.started_at,
        expires_at=row.expires_at,
        is_self=row.leader_node_id == elect.node_id,
        self_node_id=elect.node_id,
    )


@router.get("/health", response_model=HealthOut)
async def cluster_health(
    _user: str = Depends(require_permission(_PERM)),
):
    reg = get_node_registry()
    all_nodes = await reg.get_all_nodes()
    active = sum(1 for n in all_nodes if n.status == "active")
    draining = sum(1 for n in all_nodes if n.status == "draining")
    down = sum(1 for n in all_nodes if n.status == "down")
    store = await get_session_store()
    store_health = await store.health()
    router_ = get_sticky_router()
    pubsub = get_pubsub()
    rt = router_.routing_table()
    # find leader
    leader_node = None
    try:
        async with __import__("app").db.session.async_session_factory() as db:  # type: ignore
            row = (await db.execute(select(LeaderElection))).scalar_one_or_none()
            if row and row.expires_at > datetime.now(timezone.utc):
                leader_node = row.leader_node_id
    except Exception:                                               # pragma: no cover
        pass
    return HealthOut(
        cluster_size=len(all_nodes),
        active=active, draining=draining, down=down,
        leader=leader_node,
        session_store=store_health,
        pubsub_node_id=pubsub.node_id,
        ring_size=int(rt.get("ring_size", 0)),
    )


@router.post("/nodes/{node_id}/drain")
async def drain_node(
    node_id: str = Path(...),
    _user: str = Depends(require_permission(_PERM)),
):
    reg = get_node_registry()
    if node_id == reg.node_id:
        await reg.drain()
        return {"status": "draining", "node_id": node_id}
    ok = await reg.set_status(node_id, "draining")
    if not ok:
        raise HTTPException(404, "node not found")
    # also kick the sticky router to rebalance
    await get_sticky_router().rebalance_on_node_change()
    return {"status": "draining", "node_id": node_id}


@router.delete("/nodes/{node_id}")
async def remove_node(
    node_id: str = Path(...),
    _user: str = Depends(require_permission(_PERM)),
):
    reg = get_node_registry()
    if node_id == reg.node_id:
        raise HTTPException(400, "cannot force-remove self; use drain + stop")
    ok = await reg.remove_node(node_id)
    if not ok:
        raise HTTPException(404, "node not found")
    await get_sticky_router().rebalance_on_node_change()
    return {"status": "removed", "node_id": node_id}


@router.get("/routing-table")
async def routing_table(
    _user: str = Depends(require_permission(_PERM)),
):
    return get_sticky_router().routing_table()


@router.post("/rebalance")
async def manual_rebalance(
    _user: str = Depends(require_permission(_PERM)),
):
    snap = await get_sticky_router().rebalance_on_node_change()
    return {
        "status": "ok",
        "nodes": snap.nodes,
        "ring_size": len(snap.points),
    }


@router.get("/nginx-upstream", response_class=None)
async def nginx_upstream(
    upstream_name: str = "helen_cluster",
    _user: str = Depends(require_permission(_PERM)),
):
    return {"config": get_sticky_router().emit_nginx_upstream(upstream_name)}


@router.get("/haproxy-backend", response_class=None)
async def haproxy_backend(
    backend_name: str = "helen_cluster",
    _user: str = Depends(require_permission(_PERM)),
):
    return {"config": get_sticky_router().emit_haproxy_backend(backend_name)}


# ── pubsub HTTP ingestion (server-to-server) ────────────────


@router.post("/pubsub/ingest", include_in_schema=False)
async def pubsub_ingest(
    envelope: dict[str, Any] = Body(...),
    x_cluster_id: Optional[str] = Header(default=None),
):
    settings = get_settings()
    expected = settings.COMMCLIENT_CLUSTER_ID
    if expected and x_cluster_id and not hmac.compare_digest(expected, x_cluster_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "cluster_id mismatch")
    await get_pubsub().ingest(envelope)
    return {"status": "ok"}
