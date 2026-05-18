"""
Federation v2 — event DAG store & state resolution.

Each event has zero or more parents. Depth is ``max(parent.depth) + 1``.
State resolution for a given state_key prefers:
    1. higher ``depth``
    2. higher ``ts``
    3. lexicographically smaller ``event_id`` (deterministic tiebreak)

This is a simplified Matrix v2-style state resolver — good enough for
chat, presence, and channel membership without re-deriving Matrix's
full algorithm.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.session import async_session_factory
from app.models.federation_v2 import FederationEvent
from app.services.federation_v2.signing import event_hash

logger = get_logger(__name__)


class DAGStore:
    """Persistent DAG store backed by ``federation_v2_events``."""

    async def insert(self, event: dict[str, Any]) -> FederationEvent:
        """Insert an event if absent. Returns the row."""
        origin = event.get("origin") or ""
        origin_eid = event.get("event_id") or event_hash(event)
        async with async_session_factory() as db:
            existing = (await db.execute(
                select(FederationEvent).where(
                    FederationEvent.origin_server == origin,
                    FederationEvent.origin_event_id == origin_eid,
                )
            )).scalar_one_or_none()
            if existing is not None:
                return existing

            parents = event.get("prev") or []
            depth = await self._compute_depth(db, parents)

            row = FederationEvent(
                kind=event.get("type") or "message",
                origin_server=origin,
                origin_event_id=origin_eid,
                channel_address=event.get("channel"),
                sender_address=event.get("sender"),
                signed_payload=event,
                dag_parents=parents,
                depth=depth,
                processed=False,
            )
            db.add(row)
            await db.commit()
            await db.refresh(row)
            return row

    async def _compute_depth(
        self, db: AsyncSession, parent_ids: list[str]
    ) -> int:
        if not parent_ids:
            return 0
        # Bulk-fetch all parents in one query.
        rows = (await db.execute(
            select(FederationEvent.depth).where(
                FederationEvent.origin_event_id.in_(parent_ids)
            )
        )).all()
        if not rows:
            return 1
        return max(int(r[0]) for r in rows) + 1

    async def mark_processed(self, row_id: str, *, rejected: bool = False,
                             reason: Optional[str] = None) -> None:
        async with async_session_factory() as db:
            row = (await db.execute(
                select(FederationEvent).where(FederationEvent.id == row_id)
            )).scalar_one_or_none()
            if row is None:
                return
            row.processed = True
            row.processed_at = datetime.now(timezone.utc)
            row.rejected = rejected
            row.rejection_reason = reason
            await db.commit()

    async def head_events(
        self, channel: str, *, limit: int = 16
    ) -> list[FederationEvent]:
        """Frontier of the DAG for a channel — the deepest unprocessed
        leaves we hold."""
        async with async_session_factory() as db:
            rows = (await db.execute(
                select(FederationEvent)
                .where(FederationEvent.channel_address == channel)
                .order_by(desc(FederationEvent.depth))
                .limit(limit)
            )).scalars().all()
        return list(rows)

    async def backfill(
        self, channel: str, *, before_depth: Optional[int] = None,
        limit: int = 200,
    ) -> list[FederationEvent]:
        """Return events for ``channel`` walking backwards by depth."""
        async with async_session_factory() as db:
            q = select(FederationEvent).where(
                FederationEvent.channel_address == channel,
            )
            if before_depth is not None:
                q = q.where(FederationEvent.depth < before_depth)
            q = q.order_by(desc(FederationEvent.depth)).limit(limit)
            rows = (await db.execute(q)).scalars().all()
        return list(rows)


# ── state resolution ────────────────────────────────────────


def resolve_state(events: Iterable[FederationEvent]) -> dict[str, FederationEvent]:
    """Pick the winner per (kind, state_key) across a candidate set."""
    winners: dict[tuple[str, str], FederationEvent] = {}
    for e in events:
        state_key = (e.signed_payload or {}).get("state_key") or ""
        key = (e.kind, state_key)
        cur = winners.get(key)
        if cur is None or _state_pref(e, cur):
            winners[key] = e
    return {f"{k[0]}:{k[1]}": v for k, v in winners.items()}


def _state_pref(a: FederationEvent, b: FederationEvent) -> bool:
    """True if ``a`` outranks ``b``."""
    if a.depth != b.depth:
        return a.depth > b.depth
    a_ts = int((a.signed_payload or {}).get("ts") or 0)
    b_ts = int((b.signed_payload or {}).get("ts") or 0)
    if a_ts != b_ts:
        return a_ts > b_ts
    return a.origin_event_id < b.origin_event_id


# ── singleton ───────────────────────────────────────────────


_store: Optional[DAGStore] = None


def get_dag_store() -> DAGStore:
    global _store
    if _store is None:
        _store = DAGStore()
    return _store
