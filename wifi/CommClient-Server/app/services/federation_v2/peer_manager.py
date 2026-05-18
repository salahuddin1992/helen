"""
FederationPeerManager — operator-facing CRUD for the Federation Health Map.

This service is a thin, async, audit-aware façade over:

    * ``FederatedServer``        — the canonical peer row (federation_v2)
    * ``FederationPeerMeta``     — operator-facing extension (this addon)

It deliberately does NOT touch the protocol layer (handshake / DAG /
trust graph) directly — destructive operator actions go through the
existing federation_v2 entry points to keep on-the-wire behaviour
identical.

Singleton: ``get_peer_manager()``.
"""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Deque, Iterable, Optional

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import async_session_factory
from app.models.federation_peer import (
    FederationPeerMeta,
    VALID_FED_ROLES,
    VALID_HEALTH_STATES,
)
from app.models.federation_v2 import FederatedServer
from app.models.federation_event_log import FederationEventLog
from app.services.federation_v2.handshake import begin_handshake

logger = structlog.get_logger(__name__)


# ── constants ───────────────────────────────────────────────

METRICS_RETENTION_SEC = 24 * 60 * 60   # 24 h
METRICS_RESOLUTION_SEC = 1
MAX_METRICS_POINTS = METRICS_RETENTION_SEC // METRICS_RESOLUTION_SEC


@dataclass
class MetricPoint:
    ts: float
    rtt_ms: float = 0.0
    throughput_kbps: float = 0.0
    loss_pct: float = 0.0
    errors: int = 0
    in_kbps_actual: float = 0.0
    out_kbps_actual: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts":             self.ts,
            "rtt_ms":         self.rtt_ms,
            "throughput_kbps": self.throughput_kbps,
            "loss_pct":       self.loss_pct,
            "errors":         self.errors,
            "in_kbps_actual": self.in_kbps_actual,
            "out_kbps_actual": self.out_kbps_actual,
        }


class FederationPeerManager:
    """Async, audit-aware façade for federated peers."""

    def __init__(self) -> None:
        self._metrics: dict[str, Deque[MetricPoint]] = defaultdict(
            lambda: deque(maxlen=MAX_METRICS_POINTS)
        )
        self._lock = asyncio.Lock()

    # ── reads ────────────────────────────────────────────────

    async def list_peers(self) -> list[dict[str, Any]]:
        """Return joined ``FederatedServer`` + ``FederationPeerMeta`` rows."""
        async with async_session_factory() as db:
            servers = (await db.execute(
                select(FederatedServer)
            )).scalars().all()
            metas = (await db.execute(
                select(FederationPeerMeta)
            )).scalars().all()
        meta_by_sid = {m.server_id: m for m in metas}
        out: list[dict[str, Any]] = []
        for s in servers:
            m = meta_by_sid.get(s.server_id)
            out.append(self._merge(s, m))
        return out

    async def get_peer(self, peer_id: str) -> Optional[dict[str, Any]]:
        async with async_session_factory() as db:
            s = await self._lookup(db, peer_id)
            if s is None:
                return None
            m = (await db.execute(
                select(FederationPeerMeta).where(
                    FederationPeerMeta.server_id == s.server_id
                )
            )).scalar_one_or_none()
        return self._merge(s, m)

    async def get_peer_detail(self, peer_id: str) -> Optional[dict[str, Any]]:
        peer = await self.get_peer(peer_id)
        if peer is None:
            return None
        history = self.metrics_history(peer["server_id"], range_sec=3600)
        peer["metrics_history"] = [p.to_dict() for p in history]
        return peer

    # ── writes ───────────────────────────────────────────────

    async def ensure_meta(self, server_id: str) -> FederationPeerMeta:
        """Idempotent helper — make sure a meta row exists."""
        async with async_session_factory() as db:
            row = (await db.execute(
                select(FederationPeerMeta).where(
                    FederationPeerMeta.server_id == server_id
                )
            )).scalar_one_or_none()
            if row is not None:
                return row
            row = FederationPeerMeta(server_id=server_id)
            db.add(row)
            await db.commit()
            await db.refresh(row)
            return row

    async def update_peer(
        self,
        peer_id: str,
        fields: dict[str, Any],
        actor: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        allowed_server = {"status", "trust_level", "trust_score", "advertise_url"}
        allowed_meta = {
            "hostname", "ip_address", "region", "role",
            "health_state", "extra",
        }
        async with async_session_factory() as db:
            s = await self._lookup(db, peer_id)
            if s is None:
                return None
            for k, v in fields.items():
                if k in allowed_server:
                    setattr(s, k, v)
            await db.commit()

            m = await self._meta_for_locked(db, s.server_id)
            for k, v in fields.items():
                if k in allowed_meta:
                    if k == "role" and v not in VALID_FED_ROLES:
                        continue
                    if k == "health_state" and v not in VALID_HEALTH_STATES:
                        continue
                    setattr(m, k, v)
            await db.commit()
            await db.refresh(s)
            await db.refresh(m)
            await self._log(
                db, server_id=s.server_id, category="admin",
                severity="info", summary="peer_updated",
                payload={"fields": list(fields.keys())},
                actor=actor,
            )
            return self._merge(s, m)

    async def handshake(
        self, peer_id: str, actor: Optional[str] = None,
    ) -> dict[str, Any]:
        async with async_session_factory() as db:
            s = await self._lookup(db, peer_id)
            if s is None:
                return {"ok": False, "error": "not_found"}
            sid = s.server_id
        result = await begin_handshake(sid)
        ok = result is not None
        async with async_session_factory() as db:
            await self._log(
                db, server_id=sid, category="handshake",
                severity="info" if ok else "warn",
                summary="rehandshake_ok" if ok else "rehandshake_failed",
                actor=actor, success=ok,
            )
            if ok:
                m = await self._meta_for_locked(db, sid)
                m.last_handshake_at = datetime.now(timezone.utc)
                m.health_state = "healthy"
                await db.commit()
        return {"ok": ok, "server_id": sid}

    async def quarantine(
        self,
        peer_id: str,
        reason: str = "",
        actor: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        async with async_session_factory() as db:
            s = await self._lookup(db, peer_id)
            if s is None:
                return None
            m = await self._meta_for_locked(db, s.server_id)
            m.quarantined = True
            m.quarantined_reason = reason or ""
            m.quarantined_at = datetime.now(timezone.utc)
            m.health_state = "quarantined"
            s.status = "suspended"
            s.suspended_reason = reason or "quarantined"
            await db.commit()
            await self._log(
                db, server_id=s.server_id, category="admin",
                severity="warn", summary="quarantined",
                payload={"reason": reason}, actor=actor,
            )
            return self._merge(s, m)

    async def release(
        self,
        peer_id: str,
        actor: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        async with async_session_factory() as db:
            s = await self._lookup(db, peer_id)
            if s is None:
                return None
            m = await self._meta_for_locked(db, s.server_id)
            m.quarantined = False
            m.quarantined_reason = None
            m.quarantined_at = None
            if m.health_state == "quarantined":
                m.health_state = "unknown"
            if s.status == "suspended":
                s.status = "active"
                s.suspended_reason = None
            await db.commit()
            await self._log(
                db, server_id=s.server_id, category="admin",
                severity="info", summary="released",
                actor=actor,
            )
            return self._merge(s, m)

    async def set_role(
        self,
        peer_id: str,
        role: str,
        actor: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        if role not in VALID_FED_ROLES:
            raise ValueError(f"invalid_role:{role}")
        async with async_session_factory() as db:
            s = await self._lookup(db, peer_id)
            if s is None:
                return None
            m = await self._meta_for_locked(db, s.server_id)
            prev = m.role
            m.role = role
            await db.commit()
            await self._log(
                db, server_id=s.server_id, category="role_change",
                severity="warn" if role == "master" else "info",
                summary=f"role:{prev}->{role}",
                payload={"prev": prev, "next": role},
                actor=actor,
            )
            return self._merge(s, m)

    async def promote(
        self, peer_id: str, role: str = "master",
        actor: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        return await self.set_role(peer_id, role, actor)

    async def demote(
        self, peer_id: str, role: str = "follower",
        actor: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        return await self.set_role(peer_id, role, actor)

    # ── metrics buffer ───────────────────────────────────────

    def record_metric(self, server_id: str, point: MetricPoint) -> None:
        """Append a metric point — called by the federation transport
        when it gets RTT/throughput samples."""
        self._metrics[server_id].append(point)

    def metrics_history(
        self,
        server_id: str,
        range_sec: int = 3600,
    ) -> list[MetricPoint]:
        cutoff = time.time() - max(1, range_sec)
        return [p for p in self._metrics.get(server_id, ()) if p.ts >= cutoff]

    # ── internals ────────────────────────────────────────────

    async def _lookup(
        self, db: AsyncSession, peer_id: str,
    ) -> Optional[FederatedServer]:
        row = (await db.execute(
            select(FederatedServer).where(FederatedServer.id == peer_id)
        )).scalar_one_or_none()
        if row is not None:
            return row
        return (await db.execute(
            select(FederatedServer).where(FederatedServer.server_id == peer_id)
        )).scalar_one_or_none()

    async def _meta_for_locked(
        self, db: AsyncSession, server_id: str,
    ) -> FederationPeerMeta:
        row = (await db.execute(
            select(FederationPeerMeta).where(
                FederationPeerMeta.server_id == server_id
            )
        )).scalar_one_or_none()
        if row is None:
            row = FederationPeerMeta(server_id=server_id)
            db.add(row)
            await db.commit()
            await db.refresh(row)
        return row

    async def _log(
        self,
        db: AsyncSession,
        *,
        server_id: Optional[str],
        category: str,
        severity: str = "info",
        summary: str = "",
        payload: Optional[dict[str, Any]] = None,
        actor: Optional[str] = None,
        success: bool = True,
    ) -> None:
        try:
            db.add(FederationEventLog(
                server_id=server_id,
                category=category,
                severity=severity,
                summary=summary,
                actor=actor or "system",
                payload=payload or {},
                success=success,
                occurred_at=datetime.now(timezone.utc),
            ))
            await db.commit()
        except Exception as exc:  # pragma: no cover - audit best effort
            logger.warning("fedmap_audit_failed", error=str(exc))

    def _merge(
        self,
        server: FederatedServer,
        meta: Optional[FederationPeerMeta],
    ) -> dict[str, Any]:
        return {
            "id":            server.id,
            "server_id":     server.server_id,
            "hostname":      (meta.hostname if meta else "") or server.server_id,
            "ip_address":    meta.ip_address if meta else "",
            "public_key":    server.public_key,
            "advertise_url": server.advertise_url,
            "status":        server.status,
            "trust_level":   server.trust_level,
            "trust_score":   server.trust_score,
            "version":       server.version,
            "region":        meta.region if meta else "default",
            "role":          meta.role if meta else "follower",
            "health_state":  meta.health_state if meta else "unknown",
            "quarantined":   bool(meta.quarantined) if meta else False,
            "quarantined_reason": meta.quarantined_reason if meta else None,
            "last_seen":     server.last_seen,
            "last_handshake_at": meta.last_handshake_at if meta else None,
            "last_rtt_ms":   meta.last_rtt_ms if meta else 0.0,
            "last_throughput_kbps": meta.last_throughput_kbps if meta else 0.0,
            "last_loss_pct": meta.last_loss_pct if meta else 0.0,
            "last_error_count": meta.last_error_count if meta else 0,
            "shaper_rule_id": meta.shaper_rule_id if meta else None,
            "cert_id":       meta.cert_id if meta else None,
            "capabilities":  server.capabilities or {},
        }


# ── singleton ───────────────────────────────────────────────


_manager: Optional[FederationPeerManager] = None


def get_peer_manager() -> FederationPeerManager:
    global _manager
    if _manager is None:
        _manager = FederationPeerManager()
    return _manager
