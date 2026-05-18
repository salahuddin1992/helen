"""
Trace collector — records ``RouteHop`` rows as envelopes traverse
the broker fabric, and exposes aggregate views per trace.

Wiring
------
* ``RouteExecutor`` calls ``record_hop(env, action="forwarded",
  next_server_id=...)`` after publishing to the next hop.
* ``FabricSubscribers._dispatch`` calls ``record_hop(env, action=
  "delivered" | "ack_only")`` on receive.
* DLQ paths call ``record_hop(env, action="dlq", notes=reason)``.
* ``record_hop`` lazily creates the ``RouteTrace`` row on first hop
  for a given ``trace_id``.

API
---
    >>> await trace_collector.record_hop(env, action="forwarded",
    ...                                   next_server_id="s2")
    >>> trace = await trace_collector.get_trace("trace_01HX...")
    >>> # → {trace, hops, summary}

Retention
---------
``reaper_loop`` deletes traces (and their cascaded hops) older than
``RETENTION_DAYS`` (default 7). Production runs the reaper alongside
other periodic GC tasks.
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.session import async_session_factory
from app.models.route_trace import RouteHop, RouteTrace
from app.services.event_envelope import Envelope

logger = get_logger(__name__)

RETENTION_DAYS = int(os.environ.get("HELEN_TRACE_RETENTION_DAYS", "7"))
REAPER_INTERVAL_SEC = 3600  # once per hour

# Hop-id generator. Reuse envelope's ID style for consistency.
def _hop_id() -> str:
    from app.services.event_envelope import _gen_id
    return _gen_id("hop")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class TraceCollectorService:
    """Stateless service — operates on the DB directly."""

    def __init__(self) -> None:
        self._reaper_task: Optional[asyncio.Task] = None
        self._stopped = asyncio.Event()
        self._metrics = {
            "hops_recorded": 0,
            "traces_started": 0,
            "traces_completed": 0,
            "reaper_purged": 0,
        }

    # ── Recording ──────────────────────────────────────────────

    async def record_hop(
        self,
        env: Envelope,
        *,
        action: str,
        next_server_id: Optional[str] = None,
        notes: Optional[str] = None,
        ack_received: bool = False,
    ) -> None:
        """Record a hop. Best-effort; failures are logged but never
        raised (tracing must not break the data path).

        Lazy-creates the RouteTrace row on first hop for this
        trace_id. Updates outcome/total_hops/total_duration_ms when
        action is terminal (delivered | dlq | expired | max_hops |
        loop)."""
        try:
            async with async_session_factory() as db:
                await self._upsert_trace(db, env, action)
                await self._insert_hop(
                    db, env, action=action,
                    next_server_id=next_server_id, notes=notes,
                    ack_received=ack_received,
                )
                await db.commit()
                self._metrics["hops_recorded"] += 1
        except Exception as e:
            logger.debug(
                "trace_record_hop_failed",
                event_id=env.event_id, error=str(e),
            )

    async def _upsert_trace(self, db: AsyncSession, env: Envelope, action: str) -> None:
        existing = (await db.execute(
            select(RouteTrace).where(RouteTrace.trace_id == env.trace_id)
        )).scalar_one_or_none()
        if existing is None:
            mode = "chaos_chain" if env.max_hops > 16 else "production"
            db.add(RouteTrace(
                trace_id=env.trace_id,
                event_id=env.event_id,
                event_type=env.event_type,
                priority=env.priority,
                mode=mode,
                source_server_id=env.source_server_id,
                destination_server_id=env.destination_server_id,
                started_at=_utc_now(),
                outcome="in_flight",
                total_hops=0,
                total_retries=env.retry_count,
                call_id=env.call_id,
                channel_id=env.channel_id,
            ))
            self._metrics["traces_started"] += 1
            return
        # Update terminal outcome if applicable.
        terminal = action in ("delivered", "dlq", "expired", "max_hops", "loop")
        if terminal and existing.outcome == "in_flight":
            duration_ms = int(
                (_utc_now() - existing.started_at).total_seconds() * 1000
            )
            await db.execute(
                update(RouteTrace)
                .where(RouteTrace.trace_id == env.trace_id)
                .values(
                    outcome=action,
                    completed_at=_utc_now(),
                    total_hops=env.hop_index + 1,
                    total_retries=env.retry_count,
                    total_duration_ms=duration_ms,
                )
            )
            self._metrics["traces_completed"] += 1

    async def _insert_hop(
        self,
        db: AsyncSession,
        env: Envelope,
        *,
        action: str,
        next_server_id: Optional[str],
        notes: Optional[str],
        ack_received: bool,
    ) -> None:
        db.add(RouteHop(
            id=_hop_id(),
            trace_id=env.trace_id,
            span_id=env.span_id,
            parent_span_id=env.parent_span_id,
            server_id=env.current_server_id,
            hop_index=env.hop_index,
            received_at=_utc_now(),
            forwarded_at=_utc_now() if action == "forwarded" else None,
            ack_received_at=_utc_now() if ack_received else None,
            next_server_id=next_server_id,
            action=action,
            notes=notes,
        ))

    # ── Read API ───────────────────────────────────────────────

    async def get_trace(self, trace_id: str) -> Optional[dict]:
        async with async_session_factory() as db:
            trace = (await db.execute(
                select(RouteTrace).where(RouteTrace.trace_id == trace_id)
            )).scalar_one_or_none()
            if trace is None:
                return None
            hops_q = await db.execute(
                select(RouteHop)
                .where(RouteHop.trace_id == trace_id)
                .order_by(RouteHop.hop_index, RouteHop.received_at)
            )
            hops = list(hops_q.scalars().all())
        return {
            "trace_id": trace.trace_id,
            "event_id": trace.event_id,
            "event_type": trace.event_type,
            "priority": trace.priority,
            "mode": trace.mode,
            "source_server_id": trace.source_server_id,
            "destination_server_id": trace.destination_server_id,
            "outcome": trace.outcome,
            "started_at": trace.started_at.isoformat() if trace.started_at else None,
            "completed_at": (
                trace.completed_at.isoformat() if trace.completed_at else None
            ),
            "total_hops": trace.total_hops,
            "total_retries": trace.total_retries,
            "total_duration_ms": trace.total_duration_ms,
            "hops": [
                {
                    "hop_index": h.hop_index,
                    "server_id": h.server_id,
                    "span_id": h.span_id,
                    "parent_span_id": h.parent_span_id,
                    "action": h.action,
                    "next_server_id": h.next_server_id,
                    "received_at": (
                        h.received_at.isoformat() if h.received_at else None
                    ),
                    "forwarded_at": (
                        h.forwarded_at.isoformat() if h.forwarded_at else None
                    ),
                    "ack_received_at": (
                        h.ack_received_at.isoformat() if h.ack_received_at else None
                    ),
                    "notes": h.notes,
                }
                for h in hops
            ],
        }

    async def list_recent_traces(
        self, *, limit: int = 50, outcome: Optional[str] = None,
    ) -> list[dict]:
        async with async_session_factory() as db:
            q = select(RouteTrace).order_by(RouteTrace.started_at.desc()).limit(limit)
            if outcome is not None:
                q = q.where(RouteTrace.outcome == outcome)
            traces = list((await db.execute(q)).scalars().all())
        return [
            {
                "trace_id": t.trace_id,
                "event_type": t.event_type,
                "priority": t.priority,
                "mode": t.mode,
                "source": t.source_server_id,
                "destination": t.destination_server_id,
                "outcome": t.outcome,
                "started_at": t.started_at.isoformat() if t.started_at else None,
                "total_hops": t.total_hops,
                "total_duration_ms": t.total_duration_ms,
            }
            for t in traces
        ]

    # ── Retention ──────────────────────────────────────────────

    async def start_reaper_loop(self) -> None:
        if self._reaper_task is not None:
            return
        self._reaper_task = asyncio.create_task(self._reaper_loop())

    async def _reaper_loop(self) -> None:
        try:
            while not self._stopped.is_set():
                try:
                    await asyncio.wait_for(
                        self._stopped.wait(),
                        timeout=REAPER_INTERVAL_SEC,
                    )
                    return
                except asyncio.TimeoutError:
                    pass
                try:
                    purged = await self._purge_old()
                    if purged:
                        self._metrics["reaper_purged"] += purged
                        logger.info("trace_reaper_purged", count=purged)
                except Exception as e:
                    logger.warning("trace_reaper_error", error=str(e))
        except asyncio.CancelledError:
            return

    async def _purge_old(self) -> int:
        cutoff = _utc_now() - timedelta(days=RETENTION_DAYS)
        async with async_session_factory() as db:
            result = await db.execute(
                delete(RouteTrace).where(RouteTrace.started_at < cutoff)
            )
            await db.commit()
            return int(result.rowcount or 0)

    async def stop(self) -> None:
        self._stopped.set()
        if self._reaper_task is not None:
            self._reaper_task.cancel()
            try:
                await self._reaper_task
            except (asyncio.CancelledError, BaseException):
                pass
            self._reaper_task = None

    def metrics(self) -> dict:
        return dict(self._metrics)


# ── Module-level singleton ──────────────────────────────────────────

trace_collector = TraceCollectorService()
