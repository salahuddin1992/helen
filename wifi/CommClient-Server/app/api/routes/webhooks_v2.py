"""
Phase 6 / Module AF — user-facing webhook endpoints.

Mounted under ``/api/webhooks``. Each subscription is workspace-scoped via
the JWT subject; users can manage only the subscriptions they own.
"""
from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field, HttpUrl
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_log
from app.core.deps import get_current_user_id, get_db
from app.core.logging import get_logger
from app.models.webhook_v2 import (
    WebhookDelivery,
    WebhookSubscription,
)
from app.services.webhooks_v2 import event_bus, replay
from app.services.webhooks_v2.delivery_engine import delivery_engine

logger = get_logger(__name__)
router = APIRouter(prefix="/api/webhooks", tags=["webhooks-v2"])


# ── shapes ──────────────────────────────────────────────────────


class SubscriptionIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    url: HttpUrl
    events: List[str] = Field(default_factory=lambda: ["*"])
    filters: Dict[str, Any] = Field(default_factory=dict)
    workspace_id: Optional[str] = None
    secret: Optional[str] = None


class SubscriptionOut(BaseModel):
    id: str
    name: str
    url: str
    events: List[str]
    filters: Dict[str, Any]
    workspace_id: Optional[str]
    enabled: bool
    secret_preview: str
    failure_count: int
    consecutive_failures: int
    disabled_until: Optional[str]
    last_delivery_at: Optional[str]


def _to_out(sub: WebhookSubscription) -> SubscriptionOut:
    sec = sub.secret or ""
    return SubscriptionOut(
        id=sub.id, name=sub.name, url=sub.url,
        events=list(sub.events or []),
        filters=dict(sub.filters or {}),
        workspace_id=sub.workspace_id, enabled=sub.enabled,
        secret_preview=(sec[:4] + "…" + sec[-4:]) if len(sec) > 8 else "***",
        failure_count=sub.failure_count or 0,
        consecutive_failures=sub.consecutive_failures or 0,
        disabled_until=sub.disabled_until.isoformat() if sub.disabled_until else None,
        last_delivery_at=sub.last_delivery_at.isoformat() if sub.last_delivery_at else None,
    )


# ── CRUD ────────────────────────────────────────────────────────


@router.get("", response_model=List[SubscriptionOut])
async def list_my(
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    rows = (await db.execute(
        select(WebhookSubscription).where(
            WebhookSubscription.created_by == user_id,
        ).order_by(desc(WebhookSubscription.created_at))
    )).scalars().all()
    return [_to_out(r) for r in rows]


@router.post("", response_model=SubscriptionOut)
async def create(
    body: SubscriptionIn,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    secret = body.secret or secrets.token_urlsafe(32)
    sub = WebhookSubscription(
        id=uuid.uuid4().hex,
        workspace_id=body.workspace_id,
        name=body.name, url=str(body.url), secret=secret,
        events=body.events or ["*"],
        filters=body.filters or {},
        created_by=user_id,
    )
    db.add(sub)
    await db.commit()
    audit_log("webhooks_v2.created", user_id=user_id, success=True,
              details={"id": sub.id, "url": str(body.url)})
    return _to_out(sub)


@router.put("/{sub_id}", response_model=SubscriptionOut)
async def update(
    sub_id: str,
    body: SubscriptionIn,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    sub = await _load_owned(db, sub_id, user_id)
    sub.name = body.name
    sub.url = str(body.url)
    sub.events = body.events or ["*"]
    sub.filters = body.filters or {}
    if body.secret:
        sub.secret = body.secret
    if body.workspace_id is not None:
        sub.workspace_id = body.workspace_id
    await db.commit()
    audit_log("webhooks_v2.updated", user_id=user_id, success=True,
              details={"id": sub_id})
    return _to_out(sub)


@router.delete("/{sub_id}")
async def delete(
    sub_id: str,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    sub = await _load_owned(db, sub_id, user_id)
    await db.delete(sub)
    await db.commit()
    audit_log("webhooks_v2.deleted", user_id=user_id, success=True,
              details={"id": sub_id})
    return {"ok": True}


@router.post("/{sub_id}/test")
async def test(
    sub_id: str,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    sub = await _load_owned(db, sub_id, user_id)
    payload = {
        "event_id": uuid.uuid4().hex,
        "event_type": "test.ping",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "workspace_id": sub.workspace_id,
        "message": "hello from helen",
        "triggered_by": user_id,
    }
    delivery_id = await delivery_engine.enqueue(
        subscription_id=sub.id, url=sub.url, secret=sub.secret,
        event_type="test.ping", payload=payload,
    )
    return {"delivery_id": delivery_id}


@router.get("/{sub_id}/deliveries")
async def list_deliveries(
    sub_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    sub = await _load_owned(db, sub_id, user_id)
    rows = (await db.execute(
        select(WebhookDelivery).where(WebhookDelivery.subscription_id == sub.id)
        .order_by(desc(WebhookDelivery.created_at))
        .offset((page - 1) * page_size).limit(page_size)
    )).scalars().all()
    return {
        "items": [
            {
                "id": d.id, "event_type": d.event_type, "status": d.status,
                "attempt": d.attempt,
                "response_status": d.response_status,
                "latency_ms": d.latency_ms,
                "created_at": d.created_at.isoformat() if d.created_at else None,
                "delivered_at": d.delivered_at.isoformat() if d.delivered_at else None,
                "next_attempt_at": d.next_attempt_at.isoformat() if d.next_attempt_at else None,
                "error": (d.error_message or "")[:256],
            }
            for d in rows
        ],
    }


@router.post("/{sub_id}/deliveries/{delivery_id}/replay")
async def replay_one(
    sub_id: str,
    delivery_id: str,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    sub = await _load_owned(db, sub_id, user_id)
    d = (await db.execute(
        select(WebhookDelivery).where(
            WebhookDelivery.id == delivery_id,
            WebhookDelivery.subscription_id == sub.id,
        )
    )).scalar_one_or_none()
    if d is None:
        raise HTTPException(404, detail="delivery not found")
    new_id = await replay.replay_delivery(delivery_id)
    audit_log("webhooks_v2.replayed", user_id=user_id, success=True,
              details={"sub_id": sub.id, "original": delivery_id, "new": new_id})
    return {"new_delivery_id": new_id}


@router.get("/events")
async def known_events():
    return {"events": list(event_bus.KNOWN_EVENT_TYPES)}


# ── helpers ─────────────────────────────────────────────────────


async def _load_owned(
    db: AsyncSession, sub_id: str, user_id: str,
) -> WebhookSubscription:
    sub = (await db.execute(
        select(WebhookSubscription).where(WebhookSubscription.id == sub_id)
    )).scalar_one_or_none()
    if sub is None or sub.created_by != user_id:
        raise HTTPException(404, detail="webhook not found")
    return sub
