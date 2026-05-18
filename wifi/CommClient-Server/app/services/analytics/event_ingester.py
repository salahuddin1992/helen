"""
Event ingestion pipeline.

`track()` is the only entrypoint that calling code touches; everything
else (batching, scrubbing, dispatching) happens inside this module.

* Buffered batched writes every 5 seconds OR every 1000 events.
* PII scrubbing leverages :mod:`app.services.compliance.redactor` when
  available (Module Z); otherwise a tiny built-in regex scrub runs.
* Schema validation: event_name is enforced to ASCII identifier-like.
"""
from __future__ import annotations

import asyncio
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from app.core.logging import get_logger
from app.db.session import async_session_factory

logger = get_logger(__name__)


FLUSH_INTERVAL_SEC = 5
MAX_BATCH = 1_000
MAX_BUFFER = 10_000
_EVENT_NAME_RE = re.compile(r"^[a-z0-9_.-]{1,128}$")


# ───────────────────────────────────────────────────────────────────────
# PII scrub fallback
# ───────────────────────────────────────────────────────────────────────


_EMAIL_RE = re.compile(r"[\w._%+-]+@[\w.-]+\.[a-zA-Z]{2,}")
_PHONE_RE = re.compile(r"\+?\d[\d\s\-()]{7,}\d")
_CCARD_RE = re.compile(r"\b(?:\d[ -]*?){13,19}\b")


def _scrub_value(v: Any) -> Any:
    if isinstance(v, str):
        v = _EMAIL_RE.sub("<redacted_email>", v)
        v = _CCARD_RE.sub("<redacted_card>", v)
        v = _PHONE_RE.sub("<redacted_phone>", v)
        return v
    if isinstance(v, dict):
        return {k: _scrub_value(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_scrub_value(x) for x in v]
    return v


def _scrub_properties(props: dict[str, Any]) -> dict[str, Any]:
    try:
        from app.services.compliance.redactor import redact_dict  # type: ignore
        return redact_dict(props)
    except Exception:                                                   # noqa: BLE001
        return _scrub_value(props) or {}


# ───────────────────────────────────────────────────────────────────────
# In-memory buffer
# ───────────────────────────────────────────────────────────────────────


@dataclass
class _RawEvent:
    workspace_id: str
    user_id: Optional[str]
    session_id: Optional[str]
    event_name: str
    properties: dict[str, Any]
    ip: Optional[str]
    user_agent: Optional[str]
    occurred_at: datetime


class _Ingester:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buf: list[_RawEvent] = []
        self._task: asyncio.Task | None = None
        self._started = False

    def buffer_size(self) -> int:
        with self._lock:
            return len(self._buf)

    def track(self, ev: _RawEvent) -> None:
        if not _EVENT_NAME_RE.match(ev.event_name):
            logger.debug("analytics.track invalid event_name: %s", ev.event_name)
            return
        ev.properties = _scrub_properties(ev.properties or {})
        with self._lock:
            self._buf.append(ev)
            if len(self._buf) > MAX_BUFFER:
                # Drop the oldest 10% if we're flooded
                drop = MAX_BUFFER // 10
                del self._buf[:drop]
        if len(self._buf) >= MAX_BATCH:
            asyncio.create_task(self.flush())    # noqa: RUF006

    async def flush(self) -> int:
        with self._lock:
            batch = self._buf[:MAX_BATCH]
            self._buf = self._buf[MAX_BATCH:]
        if not batch:
            return 0
        try:
            from app.models.analytics import AnalyticsEvent
            async with async_session_factory() as db:
                rows = [
                    AnalyticsEvent(
                        workspace_id=e.workspace_id, user_id=e.user_id,
                        session_id=e.session_id, event_name=e.event_name,
                        properties=e.properties, ip=e.ip,
                        user_agent=e.user_agent, occurred_at=e.occurred_at,
                        ingested_at=datetime.now(timezone.utc),
                    )
                    for e in batch
                ]
                db.add_all(rows)
                await db.commit()
        except Exception as e:                                              # noqa: BLE001
            logger.error("analytics.flush failed: %s", e)
            return 0
        logger.debug("analytics.flush %d events", len(batch))
        return len(batch)

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(FLUSH_INTERVAL_SEC)
                await self.flush()
            except asyncio.CancelledError:
                await self.flush()
                raise
            except Exception as e:                                          # noqa: BLE001
                logger.error("analytics.loop error: %s", e)

    def start(self) -> None:
        if self._started:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("analytics.ingester: no running loop")
            return
        self._task = loop.create_task(self._loop())
        self._started = True
        logger.info("analytics.ingester.started interval=%ss", FLUSH_INTERVAL_SEC)

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._started = False


_ingester = _Ingester()


# ───────────────────────────────────────────────────────────────────────
# Public API
# ───────────────────────────────────────────────────────────────────────


def track(
    *,
    workspace_id: str,
    event_name: str,
    user_id: Optional[str] = None,
    properties: Optional[dict[str, Any]] = None,
    session_id: Optional[str] = None,
    ip: Optional[str] = None,
    user_agent: Optional[str] = None,
    occurred_at: Optional[datetime] = None,
) -> None:
    ev = _RawEvent(
        workspace_id=workspace_id,
        user_id=user_id,
        session_id=session_id,
        event_name=event_name,
        properties=properties or {},
        ip=ip,
        user_agent=user_agent,
        occurred_at=occurred_at or datetime.now(timezone.utc),
    )
    _ingester.track(ev)


def track_batch(events: list[dict[str, Any]], *, workspace_id: str,
                user_id: Optional[str] = None,
                ip: Optional[str] = None,
                user_agent: Optional[str] = None) -> int:
    accepted = 0
    for raw in events:
        name = raw.get("event") or raw.get("event_name")
        if not name:
            continue
        track(
            workspace_id=workspace_id,
            event_name=str(name),
            user_id=raw.get("user_id") or user_id,
            session_id=raw.get("session_id"),
            properties=raw.get("properties") or {},
            ip=ip, user_agent=user_agent,
            occurred_at=_parse_dt(raw.get("occurred_at")),
        )
        accepted += 1
    return accepted


def _parse_dt(v: Any) -> Optional[datetime]:
    if isinstance(v, datetime):
        return v
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        except Exception:                                                   # noqa: BLE001
            return None
    return None


def start_background_ingester() -> None:
    _ingester.start()


async def stop_background_ingester() -> None:
    await _ingester.stop()


async def force_flush() -> int:
    return await _ingester.flush()


def buffer_size() -> int:
    return _ingester.buffer_size()
