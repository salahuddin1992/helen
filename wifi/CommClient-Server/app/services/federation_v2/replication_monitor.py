"""
ReplicationMonitor — per-table replication-lag tracker + conflict detector.

The federation_v2 DAG (``federation_v2_events``) is the source of truth
for cross-server state; this monitor derives a per-table, per-peer lag
view *on top* of it so the Health-Map UI can render a quick matrix:

    {table: {peer: lag_ms, ...}, ...}

Lag is approximated by ``max(local.created_at) - peer.last_seen`` per
event-kind family — good enough for an operator panel, cheap to
compute, no extra wire chatter.

Conflict detector
-----------------
The DAG's state resolver is deterministic, so "conflicts" are really
*ambiguities* — events that landed with overlapping state_keys at
identical depth. The monitor surfaces them via ``conflicts()``.

Singleton: ``get_replication_monitor()``.
"""
from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import async_session_factory
from app.models.federation_v2 import (
    FederatedServer,
    FederationEvent,
)

logger = structlog.get_logger(__name__)


# Coarse table groupings — maps event ``kind`` to logical table.
KIND_TO_TABLE = {
    "message":   "messages",
    "edit":      "messages",
    "delete":    "messages",
    "membership": "channel_members",
    "presence":  "presence",
    "typing":    "presence",
    "reaction":  "reactions",
    "state":     "channel_state",
    "redaction": "messages",
}

ALL_TABLES = ("messages", "channel_members", "presence", "reactions", "channel_state")


@dataclass
class LagCell:
    peer: str
    table: str
    lag_ms: int
    last_event_at: Optional[datetime]
    peer_last_seen: Optional[datetime]
    samples: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "peer": self.peer,
            "table": self.table,
            "lag_ms": self.lag_ms,
            "last_event_at": (
                self.last_event_at.isoformat() if self.last_event_at else None
            ),
            "peer_last_seen": (
                self.peer_last_seen.isoformat() if self.peer_last_seen else None
            ),
            "samples": self.samples,
        }


@dataclass
class ConflictReport:
    channel: Optional[str]
    state_key: Optional[str]
    depth: int
    candidates: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "state_key": self.state_key,
            "depth": self.depth,
            "candidates": self.candidates,
        }


class ReplicationMonitor:
    async def lag_map(self) -> dict[str, dict[str, dict[str, Any]]]:
        """Return ``{table: {peer: cell, ...}}`` — empty cells default to zero."""
        async with async_session_factory() as db:
            peers = (await db.execute(
                select(FederatedServer)
            )).scalars().all()
            # Per (origin_server, kind) max created_at + count.
            rows = (await db.execute(
                select(
                    FederationEvent.origin_server,
                    FederationEvent.kind,
                    func.max(FederationEvent.created_at).label("last_at"),
                    func.count().label("n"),
                ).group_by(
                    FederationEvent.origin_server, FederationEvent.kind,
                )
            )).all()

        peer_last_seen = {p.server_id: p.last_seen for p in peers}
        now = datetime.now(timezone.utc)

        # bucket by (peer, table)
        bucket: dict[tuple[str, str], LagCell] = {}
        for origin_server, kind, last_at, n in rows:
            table = KIND_TO_TABLE.get(kind, "other")
            cell = bucket.get((origin_server, table))
            if last_at is None:
                continue
            # SQLite may return naive datetime; normalise to UTC.
            if last_at.tzinfo is None:
                last_at = last_at.replace(tzinfo=timezone.utc)
            peer_seen = peer_last_seen.get(origin_server)
            if peer_seen is not None and peer_seen.tzinfo is None:
                peer_seen = peer_seen.replace(tzinfo=timezone.utc)
            # lag = now - last_at  (apparent staleness)
            lag_ms = max(0, int((now - last_at).total_seconds() * 1000))
            if cell is None or (cell.last_event_at is None) or (
                last_at > cell.last_event_at
            ):
                bucket[(origin_server, table)] = LagCell(
                    peer=origin_server,
                    table=table,
                    lag_ms=lag_ms,
                    last_event_at=last_at,
                    peer_last_seen=peer_seen,
                    samples=int(n),
                )
            else:
                cell.samples += int(n)

        # densify to {table: {peer: cell-or-empty}}
        out: dict[str, dict[str, dict[str, Any]]] = {t: {} for t in ALL_TABLES}
        for (peer, table), cell in bucket.items():
            out.setdefault(table, {})[peer] = cell.to_dict()
        # zero-fill peers that have no events for a table
        for peer in peer_last_seen:
            for t in out:
                out[t].setdefault(peer, {
                    "peer": peer, "table": t, "lag_ms": 0,
                    "last_event_at": None,
                    "peer_last_seen": (
                        peer_last_seen[peer].isoformat()
                        if peer_last_seen[peer] else None
                    ),
                    "samples": 0,
                })
        return out

    async def conflicts(self, limit: int = 50) -> list[dict[str, Any]]:
        """Surface DAG ambiguities — events with overlapping state_key+depth."""
        async with async_session_factory() as db:
            rows = (await db.execute(
                select(FederationEvent)
                .where(FederationEvent.kind == "state")
                .order_by(FederationEvent.depth.desc())
                .limit(2000)
            )).scalars().all()

        bucket: dict[tuple[str, str, int], list[FederationEvent]] = defaultdict(list)
        for ev in rows:
            payload = ev.signed_payload or {}
            state_key = payload.get("state_key") or ""
            bucket[(ev.channel_address or "", state_key, ev.depth)].append(ev)

        reports: list[ConflictReport] = []
        for (chan, sk, depth), group in bucket.items():
            if len(group) <= 1:
                continue
            candidates = [
                {
                    "event_id": g.origin_event_id,
                    "origin_server": g.origin_server,
                    "processed": g.processed,
                    "rejected": g.rejected,
                    "created_at": (
                        g.created_at.isoformat() if g.created_at else None
                    ),
                }
                for g in group
            ]
            reports.append(ConflictReport(
                channel=chan or None,
                state_key=sk or None,
                depth=depth,
                candidates=candidates,
            ))
            if len(reports) >= limit:
                break
        return [r.to_dict() for r in reports]

    async def force_sync(self, peer_id: str) -> dict[str, Any]:
        """Best-effort: enqueue a full re-sync against the peer. Returns a
        descriptive payload that the WS can broadcast — the actual sync
        is driven by ``FederationTransport``."""
        from app.services.federation_v2.transport import get_transport
        async with async_session_factory() as db:
            row = (await db.execute(
                select(FederatedServer).where(FederatedServer.id == peer_id)
            )).scalar_one_or_none()
            if row is None:
                row = (await db.execute(
                    select(FederatedServer).where(
                        FederatedServer.server_id == peer_id
                    )
                )).scalar_one_or_none()
            if row is None:
                return {"ok": False, "error": "not_found"}
        try:
            transport = get_transport()
            await transport.sync_since(row.server_id, row.advertise_url, since=None)
            return {"ok": True, "server_id": row.server_id, "ts": time.time()}
        except Exception as exc:
            logger.warning("fedmap_force_sync_failed", error=str(exc))
            return {"ok": False, "error": str(exc)}


# ── singleton ───────────────────────────────────────────────


_monitor: Optional[ReplicationMonitor] = None


def get_replication_monitor() -> ReplicationMonitor:
    global _monitor
    if _monitor is None:
        _monitor = ReplicationMonitor()
    return _monitor
