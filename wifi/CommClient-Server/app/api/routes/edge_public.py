"""
Edge — client-facing endpoints.

* GET  /api/edge/route       — recommend an edge URL for the caller
* GET  /api/edge/health      — fast 200 used for latency probing
* POST /api/edge/heartbeat   — node heartbeat
* WS   /api/edge/sync        — origin ↔ edge sync stream
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Optional

from fastapi import (
    APIRouter, Depends, HTTPException, Query, Request, WebSocket,
    WebSocketDisconnect, status,
)
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db
from app.core.logging import get_logger
from app.models.edge import EdgeNode
from app.services.edge.edge_sync import get_or_create_channel
from app.services.edge.geo_router import get_geo_router
from app.services.edge.residency_enforcer import get_residency_enforcer

logger = get_logger(__name__)
router = APIRouter(prefix="/api/edge", tags=["edge"])


@router.get("/route")
async def recommend_edge(
    request: Request,
    workspace_id: Optional[str] = Query(None),
):
    client_ip = (
        request.headers.get("x-real-ip")
        or request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        or (request.client.host if request.client else "")
    )
    node = await get_geo_router().route_request(
        client_ip, workspace_id=workspace_id,
    )
    if node is None:
        return {"node": None, "fallback_to_origin": True}
    decision = await get_residency_enforcer().check_route(workspace_id, node)
    if not decision.allowed:
        return {
            "node": None,
            "fallback_to_origin": True,
            "denied_reason": decision.reason,
            "required_region": decision.required_region,
        }
    return {
        "node": {
            "node_id": node.node_id,
            "region":  node.region,
            "url":     node.public_url or node.advertise_url,
        },
        "client_ip": client_ip,
    }


@router.get("/health")
async def edge_health():
    return {"ok": True, "ts": time.time()}


class HeartbeatBody(BaseModel):
    node_id: str
    load_percent: float = 0.0
    capacity: dict[str, Any] = {}


@router.post("/heartbeat")
async def edge_heartbeat(
    payload: HeartbeatBody,
    db: AsyncSession = Depends(get_db),
):
    row = (await db.execute(
        select(EdgeNode).where(EdgeNode.node_id == payload.node_id)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="unknown_node")
    row.current_load_percent = max(0.0, min(100.0, float(payload.load_percent)))
    if payload.capacity:
        row.capacity = payload.capacity
    from datetime import datetime, timezone
    row.last_heartbeat = datetime.now(timezone.utc)
    if row.status == "down":
        row.status = "active"
    await db.commit()
    return {"ok": True}


@router.websocket("/sync")
async def edge_sync_ws(ws: WebSocket):
    """Origin ↔ edge sync stream. Authenticated via ``X-Edge-Node-ID`` +
    ``X-Edge-Token`` headers."""
    node_id = ws.headers.get("x-edge-node-id") or ""
    token = ws.headers.get("x-edge-token") or ""
    if not node_id or not token:
        await ws.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    # Token validation: simple shared secret, hex-encoded in env.
    import os
    expected = os.environ.get("HELEN_EDGE_NODE_TOKEN") or ""
    if expected and token != expected:
        await ws.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    await ws.accept()
    ch = get_or_create_channel(node_id)

    async def _send(msg: dict[str, Any]) -> None:
        await ws.send_text(json.dumps(msg, default=str))

    await ch.start_pump(_send)
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            await ch.receive(msg)
    except WebSocketDisconnect:
        await ch.stop()
        return
    except Exception as exc:
        logger.warning("edge_sync_ws_err node=%s err=%s", node_id, exc)
        await ch.stop()
        try:
            await ws.close()
        except Exception:
            pass
