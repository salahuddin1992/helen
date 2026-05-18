"""
Admin Network-Topology REST + WebSocket API.

Mounted under ``/api/admin`` with tag ``admin-topology``. Every endpoint
requires the caller's JWT to carry ``role: "admin"`` (the WebSocket variant
uses the same RBAC, via the manager's ``_authenticate``).

Endpoints
---------
::

    GET  /admin/topology/graph
    GET  /admin/topology/nodes
    GET  /admin/topology/links
    GET  /admin/topology/path
    POST /admin/topology/action
    GET  /admin/topology/jobs/{job_id}

    GET  /admin/federation/peers
    GET  /admin/overlay/sessions
    GET  /admin/p2p/dht/snapshot

    WS   /admin/ws/topology

Design
------
* The router keeps zero business logic — every method is a thin
  ``Depends(require_role("admin"))`` wrapper around the topology service
  layer (`app.services.topology`).
* Graceful degradation: when a sub-service is missing the aggregator surfaces
  ``<service>_disabled: true`` in the response — the router never raises
  500 because federation/overlay/p2p happens to be off.
* Audit-logged: every action and every state-modifying call writes to
  ``app.core.audit``.
"""

from __future__ import annotations

from typing import Any, Optional

import structlog
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    WebSocket,
    status,
)
from pydantic import BaseModel, Field

from app.core.audit import audit_log
from app.core.security_utils import require_role
from app.services.topology import (
    get_topology_actions,
    get_topology_aggregator,
    get_topology_ws_manager,
)
from app.services.topology.actions import VALID_ACTIONS
from app.services.topology.pathfinder import Pathfinder

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin-topology"])


# ─────────────────────────────────────────────────────────────
# Pydantic models
# ─────────────────────────────────────────────────────────────


class ActionRequest(BaseModel):
    node_id: str = Field(..., min_length=1, max_length=256)
    action:  str = Field(..., min_length=1, max_length=32)
    params:  Optional[dict[str, Any]] = None


class ActionResponse(BaseModel):
    job_id: str
    status: str
    node_id: str
    action: str


# ─────────────────────────────────────────────────────────────
# Topology graph + nodes + links
# ─────────────────────────────────────────────────────────────


@router.get("/topology/graph")
async def get_topology_graph(
    user_id: str = Depends(require_role("admin")),
    refresh: bool = Query(default=False, description="Force-refresh cache"),
) -> dict[str, Any]:
    """Return the aggregated `{nodes, edges, flags}` snapshot."""
    try:
        agg = get_topology_aggregator()
        graph = await agg.build_graph(force_refresh=refresh)
        audit_log("admin.topology.graph", user_id=user_id, success=True)
        return graph.to_dict()
    except Exception as e:
        logger.error("topology_graph_error", user_id=user_id, error=str(e))
        audit_log("admin.topology.graph", user_id=user_id,
                  success=False, details={"error": str(e)})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to build topology graph",
        )


@router.get("/topology/nodes")
async def list_topology_nodes(
    user_id: str = Depends(require_role("admin")),
    type:   Optional[str] = Query(default=None, description="server|router|client|…"),
    status_filter: Optional[str] = Query(
        default=None, alias="status",
        description="up|down|degraded|unknown",
    ),
    search: Optional[str] = Query(
        default=None,
        description="Substring match against id/hostname/ip",
    ),
) -> dict[str, Any]:
    """Filtered list of nodes."""
    graph = await get_topology_aggregator().build_graph()
    nodes = [n.to_dict() for n in graph.nodes]
    if type:
        t = type.lower()
        nodes = [n for n in nodes if n["type"] == t]
    if status_filter:
        s = status_filter.lower()
        nodes = [n for n in nodes if n["status"] == s]
    if search:
        q = search.lower()
        nodes = [
            n for n in nodes
            if q in (n["id"] or "").lower()
            or q in (n["hostname"] or "").lower()
            or q in (n["ip"] or "").lower()
        ]
    return {
        "nodes":  nodes,
        "count":  len(nodes),
        "flags":  graph.flags,
    }


@router.get("/topology/links")
async def list_topology_links(
    user_id: str = Depends(require_role("admin")),
    transport: Optional[str] = Query(default=None),
    min_latency: Optional[float] = Query(default=None, alias="minLatency", ge=0),
    max_latency: Optional[float] = Query(default=None, alias="maxLatency", ge=0),
) -> dict[str, Any]:
    """Filtered list of links."""
    graph = await get_topology_aggregator().build_graph()
    edges = [e.to_dict() for e in graph.edges]
    if transport:
        t = transport.lower()
        edges = [e for e in edges if e["transport"] == t]
    if min_latency is not None:
        edges = [e for e in edges if e["rtt_ms"] >= min_latency]
    if max_latency is not None:
        edges = [e for e in edges if e["rtt_ms"] <= max_latency]
    return {
        "edges": edges,
        "count": len(edges),
        "flags": graph.flags,
    }


@router.get("/topology/path")
async def topology_path(
    user_id: str = Depends(require_role("admin")),
    src: str = Query(..., description="Source node_id"),
    dst: str = Query(..., description="Destination node_id"),
    weight: str = Query(default="rtt", description="rtt|hops|loss"),
) -> dict[str, Any]:
    """Shortest path with hop-by-hop latency + transport."""
    graph = await get_topology_aggregator().build_graph()
    try:
        result = Pathfinder.find_path(graph, src, dst, weight=weight)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    audit_log(
        "admin.topology.path",
        user_id=user_id,
        success=result.found,
        details={"src": src, "dst": dst, "weight": weight, "hops": result.hop_count},
    )
    return result.to_dict()


# ─────────────────────────────────────────────────────────────
# Action runner
# ─────────────────────────────────────────────────────────────


@router.post("/topology/action", response_model=ActionResponse)
async def trigger_topology_action(
    payload: ActionRequest,
    user_id: str = Depends(require_role("admin")),
) -> ActionResponse:
    """
    Kick off a topology action (ping, traceroute, drain, restart, failover).

    Returns the ``job_id`` plus the current job status — the long-running
    work is performed in the background.
    """
    if payload.action not in VALID_ACTIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unknown action: {payload.action!r}. "
                   f"Valid: {sorted(VALID_ACTIONS)}",
        )
    actions = get_topology_actions()
    job = await actions.run_job(
        node_id=payload.node_id,
        action=payload.action,
        params=payload.params or {},
        user_id=user_id,
    )
    return ActionResponse(
        job_id=job.job_id,
        status=job.status,
        node_id=job.node_id,
        action=job.action,
    )


@router.get("/topology/jobs/{job_id}")
async def get_topology_job(
    job_id: str,
    user_id: str = Depends(require_role("admin")),
) -> dict[str, Any]:
    """Status / result of a previously started topology action."""
    job = get_topology_actions().get(job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"job {job_id!r} not found",
        )
    return job.to_dict()


# ─────────────────────────────────────────────────────────────
# Sub-service proxies
# ─────────────────────────────────────────────────────────────


@router.get("/federation/peers")
async def federation_peers(
    user_id: str = Depends(require_role("admin")),
) -> dict[str, Any]:
    """Federation-peer snapshot (cross-cluster gateways)."""
    try:
        from app.p2p.peer_federation import federation_snapshot, list_foreign
        peers = [p.to_dict() for p in list_foreign()]
        snap = federation_snapshot()
        return {
            "peers":               peers,
            "count":               len(peers),
            "federation_disabled": False,
            **snap,
        }
    except Exception as e:
        logger.info("federation_unavailable", error=str(e))
        return {
            "peers":               [],
            "count":               0,
            "federation_disabled": True,
        }


@router.get("/overlay/sessions")
async def overlay_sessions(
    user_id: str = Depends(require_role("admin")),
) -> dict[str, Any]:
    """Overlay-session snapshot."""
    try:
        from app.overlay.overlay_session import get_overlay_session_manager
        snap = get_overlay_session_manager().snapshot()
        return {**snap, "overlay_disabled": False}
    except Exception as e:
        logger.info("overlay_unavailable", error=str(e))
        return {
            "count":            0,
            "sessions":         [],
            "overlay_disabled": True,
        }


@router.get("/p2p/dht/snapshot")
async def p2p_dht_snapshot(
    user_id: str = Depends(require_role("admin")),
) -> dict[str, Any]:
    """Kademlia routing-table snapshot."""
    try:
        from app.p2p.peer_dht import dht_snapshot
        snap = dht_snapshot() or {}
        return {**snap, "dht_disabled": False}
    except Exception as e:
        logger.info("dht_unavailable", error=str(e))
        return {"local_records": 0, "dht_disabled": True}


# ─────────────────────────────────────────────────────────────
# WebSocket
# ─────────────────────────────────────────────────────────────


@router.websocket("/ws/topology")
async def ws_topology(ws: WebSocket) -> None:
    """
    Live topology event stream.

    Auth: ``?token=<jwt>`` query param OR ``Authorization: Bearer <jwt>``
    header. The token must carry ``role: "admin"``; non-admin tokens are
    closed with code 4403, missing/invalid tokens with 4401.

    Frame schema is documented in ``services.topology.ws_stream``.
    """
    manager = get_topology_ws_manager()
    await manager.handle_connection(ws)
