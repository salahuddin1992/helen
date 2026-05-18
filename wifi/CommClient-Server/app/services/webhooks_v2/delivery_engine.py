"""
Phase 6 / Module AF — webhook delivery engine.

Architecture
------------
* a single ``asyncio.Queue`` of pending deliveries
* N worker coroutines (default 4) that dequeue and ``POST`` payloads
* signed with HMAC-SHA256 (see :mod:`signing`)
* per-subscription circuit breaker (disable for 1h after 10 consecutive failures)
* exponential retry: 1s, 5s, 30s, 5min, 1h, 6h, 24h  (7 attempts)
* exhausted retries → ``webhook_v2_dead_letters``

Persistence semantics
---------------------
Every queue item also has a row in ``webhook_v2_deliveries`` so a server
restart can resume in-flight work via ``recover_pending()``.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

try:                                                                 # pragma: no cover
    import httpx                                                     # type: ignore
    _HTTPX_OK = True
except Exception:                                                    # pragma: no cover
    httpx = None                                                     # type: ignore
    _HTTPX_OK = False

from sqlalchemy import desc, select, update

from app.core.logging import get_logger
from app.db.session import async_session_factory
from app.models.webhook_v2 import (
    WebhookDeadLetter,
    WebhookDelivery,
    WebhookSubscription,
)
from app.services.webhooks_v2.signing import sign_payload

logger = get_logger(__name__)


# delays per attempt
_RETRY_DELAYS_SEC: tuple[int, ...] = (
    1, 5, 30, 300, 3600, 6 * 3600, 24 * 3600,
)
_MAX_ATTEMPTS = len(_RETRY_DELAYS_SEC)
_TIMEOUT_SEC = 10.0
_CB_THRESHOLD = 10
_CB_DISABLE_SEC = 3600


@dataclass
class _Item:
    delivery_id: str
    subscription_id: str
    url: str
    secret: str
    event_type: str
    payload: Dict[str, Any]
    attempt: int = 0


class DeliveryEngine:
    def __init__(self, worker_count: int = 4) -> None:
        self._q: asyncio.Queue[_Item] = asyncio.Queue()
        self._workers: List[asyncio.Task] = []
        self._worker_count = worker_count
        self._client: Optional[Any] = None
        self._running = False

    # ── lifecycle ────────────────────────────────────────────

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        if _HTTPX_OK:
            self._client = httpx.AsyncClient(timeout=_TIMEOUT_SEC)              # type: ignore[attr-defined]
        for i in range(self._worker_count):
            self._workers.append(asyncio.create_task(
                self._worker_loop(i), name=f"webhook_v2_worker_{i}",
            ))
        asyncio.create_task(self.recover_pending(), name="webhook_v2_recover")
        logger.info("webhook_v2_delivery_engine_started", workers=self._worker_count)

    async def stop(self) -> None:
        self._running = False
        for t in self._workers:
            if not t.done():
                t.cancel()
        for t in self._workers:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._workers.clear()
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:                                                  # pragma: no cover
                pass
            self._client = None

    # ── enqueue ──────────────────────────────────────────────

    async def enqueue(
        self,
        *,
        subscription_id: str,
        url: str,
        secret: str,
        event_type: str,
        payload: Dict[str, Any],
    ) -> str:
        delivery_id = uuid.uuid4().hex
        event_id = payload.get("event_id") or uuid.uuid4().hex
        async with async_session_factory() as db:
            db.add(WebhookDelivery(
                id=delivery_id,
                subscription_id=subscription_id,
                event_id=event_id,
                event_type=event_type,
                payload=payload,
                status="pending",
                attempt=0,
            ))
            await db.commit()
        await self._q.put(_Item(
            delivery_id=delivery_id,
            subscription_id=subscription_id,
            url=url, secret=secret,
            event_type=event_type, payload=payload, attempt=0,
        ))
        return delivery_id

    async def recover_pending(self) -> None:
        """Re-queue any delivery that was in flight when the server stopped."""
        try:
            async with async_session_factory() as db:
                rows = (await db.execute(
                    select(WebhookDelivery).where(
                        WebhookDelivery.status.in_(("pending", "in_flight"))
                    ).limit(5000)
                )).scalars().all()
                subs_by_id: Dict[str, WebhookSubscription] = {}
                for d in rows:
                    sub = subs_by_id.get(d.subscription_id)
                    if sub is None:
                        sub = (await db.execute(
                            select(WebhookSubscription).where(
                                WebhookSubscription.id == d.subscription_id,
                            )
                        )).scalar_one_or_none()
                        if sub is None:
                            continue
                        subs_by_id[d.subscription_id] = sub
                    await self._q.put(_Item(
                        delivery_id=d.id,
                        subscription_id=d.subscription_id,
                        url=sub.url, secret=sub.secret,
                        event_type=d.event_type, payload=d.payload or {},
                        attempt=d.attempt or 0,
                    ))
        except Exception as e:
            logger.warning("webhook_v2_recover_failed", error=str(e))

    # ── workers ──────────────────────────────────────────────

    async def _worker_loop(self, idx: int) -> None:
        while self._running:
            try:
                item = await self._q.get()
            except asyncio.CancelledError:
                return
            try:
                await self._deliver(item)
            except Exception as e:                                              # pragma: no cover
                logger.exception("webhook_v2_worker_error", error=str(e))

    async def _deliver(self, item: _Item) -> None:
        item.attempt += 1
        body = json.dumps(item.payload, default=str).encode("utf-8")
        sig, ts = sign_payload(
            item.secret, body, delivery_id=item.delivery_id,
            event_type=item.event_type,
        )
        headers = {
            "Content-Type": "application/json",
            "X-Helen-Event": item.event_type,
            "X-Helen-Delivery-Id": item.delivery_id,
            "X-Helen-Timestamp": str(ts),
            "X-Helen-Signature": sig,
            "X-Helen-Attempt": str(item.attempt),
            "X-Helen-Workspace": str(item.payload.get("workspace_id") or ""),
            "User-Agent": "helen-webhook/2.0",
        }

        await self._mark_in_flight(item.delivery_id, item.attempt)
        t0 = time.perf_counter()
        ok = False
        status_code: Optional[int] = None
        resp_text = ""
        error: Optional[str] = None

        if not _HTTPX_OK:
            error = "httpx not installed"
        else:
            try:
                resp = await self._client.post(item.url, content=body, headers=headers)  # type: ignore[union-attr]
                status_code = resp.status_code
                resp_text = resp.text[:2048]
                ok = 200 <= status_code < 300
            except Exception as e:
                error = str(e)[:512]

        latency_ms = int((time.perf_counter() - t0) * 1000)

        if ok:
            await self._mark_delivered(item.delivery_id, status_code, resp_text, latency_ms)
            await self._on_success(item.subscription_id)
            return

        await self._on_failure(item.subscription_id)

        if item.attempt >= _MAX_ATTEMPTS:
            await self._dead_letter(item, status_code, resp_text or error)
            return

        delay = _RETRY_DELAYS_SEC[item.attempt - 1]
        await self._schedule_retry(item, delay, status_code, resp_text or error, latency_ms)

    # ── persistence helpers ───────────────────────────────────

    async def _mark_in_flight(self, delivery_id: str, attempt: int) -> None:
        async with async_session_factory() as db:
            await db.execute(
                update(WebhookDelivery).where(WebhookDelivery.id == delivery_id).values(
                    status="in_flight", attempt=attempt,
                )
            )
            await db.commit()

    async def _mark_delivered(self, delivery_id: str, status: Optional[int],
                              body: str, latency_ms: int) -> None:
        async with async_session_factory() as db:
            await db.execute(
                update(WebhookDelivery).where(WebhookDelivery.id == delivery_id).values(
                    status="delivered",
                    response_status=status, response_body=body,
                    latency_ms=latency_ms,
                    delivered_at=datetime.now(timezone.utc),
                )
            )
            await db.commit()

    async def _schedule_retry(
        self, item: _Item, delay: int,
        status_code: Optional[int], body_or_error: str, latency_ms: int,
    ) -> None:
        next_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
        async with async_session_factory() as db:
            await db.execute(
                update(WebhookDelivery).where(WebhookDelivery.id == item.delivery_id).values(
                    status="pending",
                    response_status=status_code,
                    response_body=body_or_error,
                    latency_ms=latency_ms,
                    next_attempt_at=next_at,
                    error_message=body_or_error[:2048],
                )
            )
            await db.commit()

        async def _later():
            await asyncio.sleep(delay)
            await self._q.put(item)
        asyncio.create_task(_later())

    async def _dead_letter(self, item: _Item, status_code: Optional[int], reason: str) -> None:
        async with async_session_factory() as db:
            await db.execute(
                update(WebhookDelivery).where(WebhookDelivery.id == item.delivery_id).values(
                    status="dead",
                    response_status=status_code,
                    error_message=(reason or "")[:2048],
                )
            )
            db.add(WebhookDeadLetter(
                id=uuid.uuid4().hex,
                delivery_id=item.delivery_id,
                subscription_id=item.subscription_id,
                reason=(reason or "max attempts")[:255],
                body=item.payload,
            ))
            await db.commit()
        logger.warning("webhook_v2_dead_letter",
                       delivery_id=item.delivery_id, sub_id=item.subscription_id)

    async def _on_success(self, subscription_id: str) -> None:
        async with async_session_factory() as db:
            await db.execute(
                update(WebhookSubscription).where(
                    WebhookSubscription.id == subscription_id,
                ).values(
                    last_delivery_at=datetime.now(timezone.utc),
                    consecutive_failures=0,
                )
            )
            await db.commit()

    async def _on_failure(self, subscription_id: str) -> None:
        async with async_session_factory() as db:
            sub = (await db.execute(
                select(WebhookSubscription).where(WebhookSubscription.id == subscription_id)
            )).scalar_one_or_none()
            if sub is None:
                return
            sub.consecutive_failures = (sub.consecutive_failures or 0) + 1
            sub.failure_count = (sub.failure_count or 0) + 1
            if sub.consecutive_failures >= _CB_THRESHOLD:
                sub.disabled_until = datetime.now(timezone.utc) + timedelta(seconds=_CB_DISABLE_SEC)
                sub.consecutive_failures = 0
                logger.warning(
                    "webhook_v2_circuit_open",
                    sub_id=subscription_id, until=sub.disabled_until.isoformat(),
                )
            await db.commit()


delivery_engine = DeliveryEngine()
