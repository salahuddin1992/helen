"""
Webhook service — manage subscriptions, queue + deliver events with HMAC
signatures, retries with exponential backoff.

Delivery is HTTP POST with these headers:
    Content-Type: application/json
    X-CommClient-Event: <event-name>
    X-CommClient-Delivery: <delivery_id>
    X-CommClient-Signature: sha256=<hex hmac of body using webhook.secret>
    X-CommClient-Timestamp: <epoch seconds>

A background loop (`run_dispatch_loop`) drains the pending queue, retries
failed deliveries with exponential backoff, and gives up after 6 attempts.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.db.session import async_session_factory
from app.models.webhook import Webhook, WebhookDelivery

logger = get_logger(__name__)

_MAX_ATTEMPTS = 6
_POLL_INTERVAL_SEC = 10
_REQUEST_TIMEOUT_SEC = 15.0
# Disable a webhook after this many consecutive total failures across deliveries
_DISABLE_AFTER_FAILURES = 25


def _backoff(attempt: int) -> timedelta:
    """Exponential backoff: 30s, 1m, 2m, 4m, 8m, 16m."""
    return timedelta(seconds=30 * (2 ** max(0, attempt - 1)))


def _normalize_events(events: str | list[str] | None) -> str:
    if events is None:
        return "*"
    if isinstance(events, list):
        if not events:
            return "*"
        items = [e.strip() for e in events if e and e.strip()]
        return ",".join(sorted(set(items))) if items else "*"
    return events.strip() or "*"


def _matches(subscribed: str, event: str) -> bool:
    if subscribed == "*":
        return True
    parts = {p.strip() for p in subscribed.split(",") if p.strip()}
    if event in parts:
        return True
    # Wildcard suffix support: "channel.*" matches "channel.created"
    for p in parts:
        if p.endswith(".*") and event.startswith(p[:-1]):
            return True
    return False


def _sign(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _sign_v2(secret: str, timestamp: int, body: bytes) -> str:
    """Replay-safe signature: HMAC over ``<timestamp>.<body>`` so an
    intercepted webhook can't be replayed indefinitely (receivers reject
    timestamps too far from now). Stripe-style format."""
    payload = str(timestamp).encode("ascii") + b"." + body
    digest = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


class WebhookService:
    """CRUD + dispatch for outbound webhooks."""

    # ── CRUD ──────────────────────────────────────────────────

    @staticmethod
    async def create(
        db: AsyncSession,
        owner_id: str,
        name: str,
        url: str,
        events: list[str] | str | None = None,
        channel_id: str | None = None,
        secret: str | None = None,
    ) -> Webhook:
        if not name or len(name) > 128:
            raise ValidationError("name is required (≤128 chars)")
        if not url or not (url.startswith("http://") or url.startswith("https://")):
            raise ValidationError("url must be http(s)://")
        if len(url) > 2048:
            raise ValidationError("url too long")
        secret = secret or secrets.token_urlsafe(32)
        rec = Webhook(
            owner_id=owner_id,
            name=name,
            url=url,
            secret=secret,
            events=_normalize_events(events),
            channel_id=channel_id,
            is_active=True,
        )
        db.add(rec)
        await db.commit()
        await db.refresh(rec)
        logger.info(
            "webhook_created",
            webhook_id=rec.id,
            owner_id=owner_id,
            events=rec.events,
        )
        return rec

    @staticmethod
    async def list_for_owner(db: AsyncSession, owner_id: str) -> list[Webhook]:
        result = await db.execute(
            select(Webhook).where(Webhook.owner_id == owner_id).order_by(Webhook.created_at.desc())
        )
        return list(result.scalars().all())

    @staticmethod
    async def get(db: AsyncSession, webhook_id: str, owner_id: str) -> Webhook:
        result = await db.execute(
            select(Webhook).where(
                and_(Webhook.id == webhook_id, Webhook.owner_id == owner_id)
            )
        )
        rec = result.scalar_one_or_none()
        if rec is None:
            raise NotFoundError("Webhook", webhook_id)
        return rec

    @staticmethod
    async def update(
        db: AsyncSession,
        webhook_id: str,
        owner_id: str,
        *,
        name: str | None = None,
        url: str | None = None,
        events: list[str] | str | None = None,
        is_active: bool | None = None,
        channel_id: str | None = None,
    ) -> Webhook:
        rec = await WebhookService.get(db, webhook_id, owner_id)
        if name is not None:
            if not name or len(name) > 128:
                raise ValidationError("name length 1-128")
            rec.name = name
        if url is not None:
            if not (url.startswith("http://") or url.startswith("https://")):
                raise ValidationError("url must be http(s)://")
            rec.url = url
        if events is not None:
            rec.events = _normalize_events(events)
        if is_active is not None:
            rec.is_active = is_active
            if is_active:
                rec.consecutive_failures = 0
        if channel_id is not None:
            rec.channel_id = channel_id or None
        await db.commit()
        await db.refresh(rec)
        return rec

    @staticmethod
    async def delete(db: AsyncSession, webhook_id: str, owner_id: str) -> None:
        rec = await WebhookService.get(db, webhook_id, owner_id)
        await db.delete(rec)
        await db.commit()

    # ── Enqueue ───────────────────────────────────────────────

    @staticmethod
    async def emit(
        db: AsyncSession,
        event: str,
        payload: dict[str, Any],
        channel_id: str | None = None,
    ) -> int:
        """
        Queue an event for delivery to all matching webhooks.
        Returns the number of deliveries enqueued.
        """
        # Find all active webhooks that match the event/channel
        stmt = select(Webhook).where(Webhook.is_active == True)  # noqa: E712
        if channel_id is not None:
            # match webhooks scoped to this channel OR scoped to no channel
            from sqlalchemy import or_

            stmt = stmt.where(
                or_(Webhook.channel_id == channel_id, Webhook.channel_id.is_(None))
            )
        else:
            stmt = stmt.where(Webhook.channel_id.is_(None))
        result = await db.execute(stmt)
        webhooks = list(result.scalars().all())

        body = json.dumps(payload, default=str)
        now = datetime.now(timezone.utc)
        enqueued = 0
        for wh in webhooks:
            if not _matches(wh.events, event):
                continue
            db.add(
                WebhookDelivery(
                    webhook_id=wh.id,
                    event=event,
                    payload_json=body,
                    status="pending",
                    attempt_count=0,
                    next_attempt_at=now,
                )
            )
            enqueued += 1
        if enqueued:
            await db.commit()
            logger.info("webhook_events_enqueued", event_name=event, count=enqueued)
        return enqueued

    # ── Dispatch ──────────────────────────────────────────────

    @staticmethod
    async def _claim_due(
        db: AsyncSession, limit: int = 25
    ) -> list[WebhookDelivery]:
        now = datetime.now(timezone.utc)
        result = await db.execute(
            select(WebhookDelivery)
            .where(
                and_(
                    WebhookDelivery.status == "pending",
                    WebhookDelivery.next_attempt_at <= now,
                )
            )
            .order_by(WebhookDelivery.next_attempt_at.asc())
            .limit(limit)
        )
        return list(result.scalars().all())

    @staticmethod
    async def _deliver_one(
        db: AsyncSession, delivery: WebhookDelivery
    ) -> bool:
        wh = await db.get(Webhook, delivery.webhook_id)
        if wh is None or not wh.is_active:
            delivery.status = "dead"
            delivery.last_error = "webhook missing or disabled"
            await db.commit()
            return False

        body = delivery.payload_json.encode("utf-8")
        ts = int(time.time())
        signature = _sign(wh.secret, body)
        signature_v2 = _sign_v2(wh.secret, ts, body)
        headers = {
            "Content-Type": "application/json",
            "X-CommClient-Event": delivery.event,
            "X-CommClient-Delivery": delivery.id,
            # v1 = body-only HMAC (kept for backwards compat with
            # existing receivers). v2 covers timestamp.body so a
            # replay attack on an intercepted webhook is detectable
            # by any receiver that tracks accepted timestamps.
            "X-CommClient-Signature": signature,
            "X-CommClient-Signature-V2": signature_v2,
            "X-CommClient-Timestamp": str(ts),
        }

        delivery.attempt_count += 1
        try:
            # Use the shared connection pool — opening + tearing down a
            # fresh AsyncClient per delivery wastes 1 TCP+TLS handshake
            # per webhook URL (was costing ~50ms each on a busy fanout
            # and exhausted file descriptors under sustained load).
            from app.services.http_connection_pool import get_pool
            client = await get_pool().client_for(
                wh.url, timeout=_REQUEST_TIMEOUT_SEC,
            )
            # client is bound to wh.url's base; POST with empty path.
            resp = await client.post("", content=body, headers=headers)
            delivery.last_status_code = resp.status_code
            ok = 200 <= resp.status_code < 300
        except httpx.HTTPError as e:
            ok = False
            delivery.last_status_code = None
            delivery.last_error = f"{type(e).__name__}: {e}"[:512]

        now = datetime.now(timezone.utc)
        wh.last_delivery_at = now
        if ok:
            delivery.status = "success"
            delivery.delivered_at = now
            delivery.last_error = None
            wh.consecutive_failures = 0
            wh.last_status = delivery.last_status_code
            wh.last_error = None
        else:
            wh.consecutive_failures = (wh.consecutive_failures or 0) + 1
            wh.last_status = delivery.last_status_code
            wh.last_error = (delivery.last_error or f"http {delivery.last_status_code}")[:512]
            if delivery.attempt_count >= _MAX_ATTEMPTS:
                delivery.status = "dead"
                # Persist the exhausted delivery to the DLQ so operators
                # can inspect and replay after fixing the endpoint. Keep
                # the captured payload small enough to survive truncation.
                try:
                    from app.services.dead_letter_service import record as _dlq_record
                    try:
                        body_payload = json.loads(delivery.payload_json)
                    except Exception:
                        body_payload = {}
                    await _dlq_record(
                        kind="webhook",
                        reason="webhook_delivery_exhausted",
                        error=wh.last_error,
                        payload={
                            "webhook_id": wh.id,
                            "url": wh.url,
                            "event": delivery.event,
                            "delivery_id": delivery.id,
                            "payload": body_payload,
                            "channel_id": wh.channel_id,
                        },
                        channel_id=wh.channel_id,
                    )
                except Exception:
                    pass
            else:
                delivery.status = "pending"
                delivery.next_attempt_at = now + _backoff(delivery.attempt_count)
            if wh.consecutive_failures >= _DISABLE_AFTER_FAILURES:
                wh.is_active = False
                logger.warning(
                    "webhook_auto_disabled",
                    webhook_id=wh.id,
                    failures=wh.consecutive_failures,
                )

        await db.commit()
        return ok

    @staticmethod
    async def run_dispatch_loop(stop_event: asyncio.Event) -> None:
        """Background loop — drain pending deliveries with bounded concurrency."""
        logger.info("webhook_dispatch_loop_started")
        while not stop_event.is_set():
            try:
                async with async_session_factory() as db:
                    due = await WebhookService._claim_due(db, limit=25)
                    if due:
                        for delivery in due:
                            try:
                                await WebhookService._deliver_one(db, delivery)
                            except Exception as e:
                                logger.warning(
                                    "webhook_delivery_error",
                                    delivery_id=delivery.id,
                                    error=str(e),
                                )
            except Exception as e:
                logger.warning("webhook_dispatch_loop_error", error=str(e))
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=_POLL_INTERVAL_SEC)
            except asyncio.TimeoutError:
                pass
        logger.info("webhook_dispatch_loop_stopped")
