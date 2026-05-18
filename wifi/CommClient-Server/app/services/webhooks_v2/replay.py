"""
Phase 6 / Module AF — replay helpers.

Operations:

* ``replay_delivery(delivery_id)`` — clone the failed delivery and re-queue.
* ``replay_subscription(subscription_id, since)`` — re-deliver every event
  fired against the subscription since ``since`` (defaults to the last 24h).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from app.core.logging import get_logger
from app.db.session import async_session_factory
from app.models.webhook_v2 import WebhookDelivery, WebhookSubscription
from app.services.webhooks_v2.delivery_engine import delivery_engine

logger = get_logger(__name__)


async def replay_delivery(delivery_id: str) -> str:
    async with async_session_factory() as db:
        d = (await db.execute(
            select(WebhookDelivery).where(WebhookDelivery.id == delivery_id)
        )).scalar_one_or_none()
        if d is None:
            raise LookupError(delivery_id)
        sub = (await db.execute(
            select(WebhookSubscription).where(
                WebhookSubscription.id == d.subscription_id,
            )
        )).scalar_one_or_none()
        if sub is None:
            raise LookupError("subscription gone")
    new_id = await delivery_engine.enqueue(
        subscription_id=sub.id, url=sub.url, secret=sub.secret,
        event_type=d.event_type, payload=d.payload or {},
    )
    logger.info("webhook_v2_replayed", original=delivery_id, new=new_id)
    return new_id


async def replay_subscription(
    subscription_id: str,
    *,
    since: Optional[datetime] = None,
    limit: int = 500,
) -> List[str]:
    if since is None:
        since = datetime.now(timezone.utc) - timedelta(hours=24)
    async with async_session_factory() as db:
        sub = (await db.execute(
            select(WebhookSubscription).where(WebhookSubscription.id == subscription_id)
        )).scalar_one_or_none()
        if sub is None:
            raise LookupError(subscription_id)
        rows = (await db.execute(
            select(WebhookDelivery).where(
                WebhookDelivery.subscription_id == subscription_id,
                WebhookDelivery.created_at >= since,
                WebhookDelivery.status.in_(("delivered", "failed", "dead")),
            ).order_by(WebhookDelivery.created_at.desc()).limit(limit)
        )).scalars().all()
    new_ids: List[str] = []
    for d in rows:
        new_id = await delivery_engine.enqueue(
            subscription_id=sub.id, url=sub.url, secret=sub.secret,
            event_type=d.event_type, payload=d.payload or {},
        )
        new_ids.append(new_id)
    return new_ids
