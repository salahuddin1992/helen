"""
Phase 5 / Module Y — Bridge admin REST endpoints.

Mounted under ``/api/admin/bridges``. Requires ``bridges.manage`` permission.
"""
from __future__ import annotations

import secrets
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_log
from app.core.deps import get_db
from app.core.logging import get_logger
from app.services.rbac.enforcer import require_permission
from app.models.bridge import (
    BridgeConfig, BridgeMessage, VALID_BRIDGE_KINDS,
)
from app.services.bridges.base import BridgeRegistry
from app.services.bridges.dispatcher import dispatcher

logger = get_logger(__name__)

router = APIRouter(prefix="/api/admin/bridges", tags=["admin-bridges"])

_PERM_MANAGE = "bridges.manage"


# ── shapes ──────────────────────────────────────────────────


class BridgeIn(BaseModel):
    workspace_id: str
    kind: str = Field(..., description="discord | telegram | slack")
    name: str = Field(min_length=1, max_length=128)
    channel_helen_id: str
    channel_remote_id: str
    enabled: bool = False
    settings: dict[str, Any] = Field(default_factory=dict)


class BridgeUpdate(BaseModel):
    name: Optional[str] = None
    enabled: Optional[bool] = None
    channel_helen_id: Optional[str] = None
    channel_remote_id: Optional[str] = None
    settings: Optional[dict[str, Any]] = None


class BridgeOut(BaseModel):
    id: str
    workspace_id: str
    kind: str
    name: str
    enabled: bool
    channel_helen_id: str
    channel_remote_id: str
    settings: dict[str, Any]
    last_status: Optional[str]
    last_error: Optional[str]
    last_health_at: Optional[datetime]
    created_at: datetime

    @classmethod
    def from_orm_row(cls, r: BridgeConfig) -> "BridgeOut":
        # mask sensitive setting values before returning
        masked = _mask_secrets(r.settings or {})
        return cls(
            id=r.id, workspace_id=r.workspace_id, kind=r.kind,
            name=r.name, enabled=r.enabled,
            channel_helen_id=r.channel_helen_id,
            channel_remote_id=r.channel_remote_id,
            settings=masked,
            last_status=r.last_status, last_error=r.last_error,
            last_health_at=r.last_health_at, created_at=r.created_at,
        )


_SECRET_KEYS = {"bot_token", "app_token", "webhook_url", "client_secret"}


def _mask_secrets(s: dict[str, Any]) -> dict[str, Any]:
    return {k: ("***" if k in _SECRET_KEYS and v else v) for k, v in s.items()}


# ── endpoints ───────────────────────────────────────────────


@router.get("", response_model=list[BridgeOut])
async def list_bridges(
    workspace_id: Optional[str] = Query(default=None),
    kind: Optional[str] = Query(default=None),
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM_MANAGE)),
):
    q = select(BridgeConfig)
    if workspace_id:
        q = q.where(BridgeConfig.workspace_id == workspace_id)
    if kind:
        q = q.where(BridgeConfig.kind == kind)
    rows = (await db.execute(q.order_by(BridgeConfig.created_at.desc()))).scalars().all()
    return [BridgeOut.from_orm_row(r) for r in rows]


@router.post("", response_model=BridgeOut, status_code=status.HTTP_201_CREATED)
async def create_bridge(
    body: BridgeIn,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM_MANAGE)),
):
    if body.kind not in VALID_BRIDGE_KINDS:
        raise HTTPException(400, detail=f"unknown kind: {body.kind}")
    if body.kind not in BridgeRegistry.list_kinds():
        raise HTTPException(503, detail=f"bridge kind not loaded: {body.kind}")

    row = BridgeConfig(
        id=secrets.token_hex(16),
        workspace_id=body.workspace_id,
        kind=body.kind,
        name=body.name,
        enabled=body.enabled,
        channel_helen_id=body.channel_helen_id,
        channel_remote_id=body.channel_remote_id,
        settings=body.settings,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)

    if body.enabled:
        try:
            await dispatcher.start_bridge(row)
        except Exception as e:                                  # pragma: no cover
            logger.error("bridge_autostart_failed", err=str(e))

    audit_log("bridge.created", user_id=user_id, success=True,
              details={"bridge_id": row.id, "kind": row.kind})
    return BridgeOut.from_orm_row(row)


@router.put("/{bridge_id}", response_model=BridgeOut)
async def update_bridge(
    bridge_id: str,
    body: BridgeUpdate,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM_MANAGE)),
):
    row = (await db.execute(
        select(BridgeConfig).where(BridgeConfig.id == bridge_id)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "bridge not found")

    if body.name is not None:
        row.name = body.name
    if body.enabled is not None:
        row.enabled = body.enabled
    if body.channel_helen_id is not None:
        row.channel_helen_id = body.channel_helen_id
    if body.channel_remote_id is not None:
        row.channel_remote_id = body.channel_remote_id
    if body.settings is not None:
        merged = dict(row.settings or {})
        merged.update(body.settings)
        row.settings = merged
    await db.commit()
    await db.refresh(row)
    await dispatcher.reload_bridge(row)
    audit_log("bridge.updated", user_id=user_id, success=True,
              details={"bridge_id": bridge_id})
    return BridgeOut.from_orm_row(row)


@router.delete("/{bridge_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_bridge(
    bridge_id: str,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM_MANAGE)),
):
    row = (await db.execute(
        select(BridgeConfig).where(BridgeConfig.id == bridge_id)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "bridge not found")
    await dispatcher.stop_bridge(bridge_id)
    await db.delete(row)
    await db.commit()
    audit_log("bridge.deleted", user_id=user_id, success=True,
              details={"bridge_id": bridge_id})
    return None


@router.post("/{bridge_id}/start")
async def start_bridge(
    bridge_id: str,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM_MANAGE)),
):
    row = (await db.execute(
        select(BridgeConfig).where(BridgeConfig.id == bridge_id)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "bridge not found")
    row.enabled = True
    await db.commit()
    await dispatcher.reload_bridge(row)
    audit_log("bridge.started", user_id=user_id, success=True,
              details={"bridge_id": bridge_id})
    return {"ok": True}


@router.post("/{bridge_id}/stop")
async def stop_bridge(
    bridge_id: str,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM_MANAGE)),
):
    row = (await db.execute(
        select(BridgeConfig).where(BridgeConfig.id == bridge_id)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "bridge not found")
    row.enabled = False
    await db.commit()
    await dispatcher.stop_bridge(bridge_id)
    audit_log("bridge.stopped", user_id=user_id, success=True,
              details={"bridge_id": bridge_id})
    return {"ok": True}


@router.post("/{bridge_id}/test")
async def test_bridge(
    bridge_id: str,
    user_id: str = Depends(require_permission(_PERM_MANAGE)),
):
    h = await dispatcher.health(bridge_id)
    if h is None:
        raise HTTPException(409, "bridge is not currently running")
    audit_log("bridge.tested", user_id=user_id, success=h.ok,
              details={"bridge_id": bridge_id, "detail": h.detail})
    return {"ok": h.ok, "detail": h.detail, "extra": h.extra}


@router.get("/{bridge_id}/health")
async def get_health(
    bridge_id: str,
    _user: str = Depends(require_permission(_PERM_MANAGE)),
):
    h = await dispatcher.health(bridge_id)
    if h is None:
        return {"ok": False, "detail": "not running", "extra": {}}
    return {"ok": h.ok, "detail": h.detail, "extra": h.extra}


@router.get("/{bridge_id}/messages")
async def list_messages(
    bridge_id: str,
    limit: int = Query(50, ge=1, le=500),
    direction: Optional[str] = Query(default=None),
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM_MANAGE)),
):
    q = select(BridgeMessage).where(BridgeMessage.bridge_id == bridge_id)
    if direction:
        q = q.where(BridgeMessage.direction == direction)
    q = q.order_by(desc(BridgeMessage.created_at)).limit(limit)
    rows = (await db.execute(q)).scalars().all()
    return [{
        "id": r.id,
        "direction": r.direction,
        "status": r.status,
        "helen_message_id": r.helen_message_id,
        "remote_message_id": r.remote_message_id,
        "error": r.error,
        "created_at": r.created_at,
    } for r in rows]


@router.get("/_runtime/status")
async def runtime_status(
    _user: str = Depends(require_permission(_PERM_MANAGE)),
):
    return {
        "registered_kinds": BridgeRegistry.list_kinds(),
        "running": dispatcher.status_table(),
    }
