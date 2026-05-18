"""Webhook dispatcher — fans alert events out to operator-configured URLs.

Subscribes to ``alert.fired`` and ``alert.cleared`` on the monitoring
event bus and POSTs JSON to every configured webhook in parallel.
Failures are logged but never block the event handler.

Configuration: ``HELEN_ALERT_WEBHOOKS=https://hooks.x/y,https://...``
(comma-separated). Each URL receives:

    {
      "event":   "alert.fired" | "alert.cleared",
      "name":    "<rule name>",
      "detail":  "<rule detail>",
      "ts":      <unix>,
      "source":  "helen-server",
    }
"""

from __future__ import annotations

import asyncio
import os
import threading
import time

from app.core.logging import get_logger
from app.monitoring.monitoring_events import subscribe

logger = get_logger(__name__)


def _parse_webhooks() -> list[str]:
    raw = os.environ.get("HELEN_ALERT_WEBHOOKS", "") or ""
    return [u.strip() for u in raw.split(",") if u.strip()]


class _Stats:
    _lock = threading.Lock()
    fired_count = 0
    delivered_count = 0
    failed_count = 0
    last_delivery_at: float = 0.0


def _record(success: bool) -> None:
    with _Stats._lock:
        _Stats.fired_count += 1
        if success:
            _Stats.delivered_count += 1
        else:
            _Stats.failed_count += 1
        _Stats.last_delivery_at = time.time()


async def _post_one(url: str, payload: dict) -> bool:
    try:
        import httpx
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.post(url, json=payload)
        return 200 <= r.status_code < 300
    except Exception as e:
        logger.debug("webhook_post_failed", url=url[:60], error=str(e)[:80])
        return False


async def _fanout(payload: dict) -> None:
    urls = _parse_webhooks()
    if not urls:
        return
    results = await asyncio.gather(
        *(_post_one(u, payload) for u in urls),
        return_exceptions=True,
    )
    for r in results:
        _record(bool(r) if not isinstance(r, BaseException) else False)


def _on_alert(event_name: str, payload: dict) -> None:
    if event_name not in ("alert.fired", "alert.cleared"):
        return
    body = {
        "event":  event_name,
        "name":   payload.get("name"),
        "detail": payload.get("detail"),
        "ts":     time.time(),
        "source": "helen-server",
    }
    try:
        loop = asyncio.get_event_loop()
        loop.create_task(_fanout(body))
    except RuntimeError:
        # No running loop — fire-and-forget thread.
        threading.Thread(
            target=lambda: asyncio.run(_fanout(body)),
            daemon=True,
        ).start()


_subscribed = False


def install() -> None:
    """Subscribe to alert events. Idempotent."""
    global _subscribed
    if _subscribed:
        return
    subscribe("alert.fired",   _on_alert)
    subscribe("alert.cleared", _on_alert)
    _subscribed = True
    logger.info("webhook_dispatcher_installed",
                urls_configured=len(_parse_webhooks()))


def status() -> dict:
    with _Stats._lock:
        return {
            "subscribed":         _subscribed,
            "configured_urls":    len(_parse_webhooks()),
            "fired_count":        _Stats.fired_count,
            "delivered_count":    _Stats.delivered_count,
            "failed_count":       _Stats.failed_count,
            "last_delivery_at":   _Stats.last_delivery_at,
        }
