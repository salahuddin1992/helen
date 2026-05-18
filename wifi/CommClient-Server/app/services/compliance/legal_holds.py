"""
ComplianceLegalHoldsService — eDiscovery legal hold management.

Capabilities:
* Create/list/get/release holds with scope conflict detection.
* ``is_under_hold(resource_type, resource_id, subject_id, timestamp)``:
  resolves *whether* a given resource is currently frozen by a hold.
* Per-hold audit trail.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy import and_, desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_log
from app.core.logging import get_logger
from app.db.session import async_session_factory
from app.models.compliance_hold import (
    VALID_HOLD_STATUSES,
    ComplianceHold,
    ComplianceHoldAudit,
)

logger = get_logger(__name__)


# ── helpers ─────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_scope(scope: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    scope = scope or {}
    out: Dict[str, Any] = {
        "custodians":    list(scope.get("custodians") or []),
        "channels":      list(scope.get("channels") or []),
        "keywords":      list(scope.get("keywords") or []),
        "file_types":    [str(x).lstrip(".").lower() for x in (scope.get("file_types") or [])],
        "message_types": list(scope.get("message_types") or []),
    }
    dr = scope.get("date_range") or {}
    if isinstance(dr, dict) and (dr.get("start") or dr.get("end")):
        out["date_range"] = {
            "start": dr.get("start"),
            "end":   dr.get("end"),
        }
    return out


def _scope_overlaps(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    """Heuristic conflict detection: holds overlap when their custodians
    or channels intersect AND their date ranges (if any) intersect."""
    a_cust = set(a.get("custodians") or [])
    b_cust = set(b.get("custodians") or [])
    a_chan = set(a.get("channels") or [])
    b_chan = set(b.get("channels") or [])
    overlap = (
        bool(a_cust & b_cust)
        or bool(a_chan & b_chan)
        or (not a_cust and not a_chan)
        or (not b_cust and not b_chan)
    )
    if not overlap:
        return False
    a_dr = a.get("date_range") or {}
    b_dr = b.get("date_range") or {}
    if not a_dr and not b_dr:
        return True
    try:
        a_s = _to_dt(a_dr.get("start")) if a_dr else None
        a_e = _to_dt(a_dr.get("end")) if a_dr else None
        b_s = _to_dt(b_dr.get("start")) if b_dr else None
        b_e = _to_dt(b_dr.get("end")) if b_dr else None
    except Exception:
        return True
    # interval overlap if start1 <= end2 and start2 <= end1
    if a_s and b_e and a_s > b_e:
        return False
    if b_s and a_e and b_s > a_e:
        return False
    return True


def _to_dt(v: Any) -> Optional[datetime]:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _hold_matches(
    hold: ComplianceHold,
    *,
    resource_type: str,
    resource_id: str,
    subject_id: Optional[str],
    timestamp: Optional[datetime],
    channel_id: Optional[str],
    file_type: Optional[str],
    message_type: Optional[str],
    keywords_present: Optional[Iterable[str]] = None,
) -> bool:
    if hold.status != "active":
        return False
    if hold.expires_at and hold.expires_at < _now():
        return False
    scope = hold.scope or {}

    custodians = set(scope.get("custodians") or [])
    if custodians and subject_id and subject_id not in custodians:
        # If holds list custodians but the subject is not in it, skip.
        # If subject_id is None, we treat it as unknown — match by other dims.
        return False

    channels = set(scope.get("channels") or [])
    if channels:
        if not channel_id or channel_id not in channels:
            return False

    file_types = set(scope.get("file_types") or [])
    if file_types:
        if not file_type or file_type.lstrip(".").lower() not in file_types:
            return False

    message_types = set(scope.get("message_types") or [])
    if message_types:
        if not message_type or message_type not in message_types:
            return False

    dr = scope.get("date_range") or {}
    if dr and timestamp:
        ds = _to_dt(dr.get("start"))
        de = _to_dt(dr.get("end"))
        if ds and timestamp < ds:
            return False
        if de and timestamp > de:
            return False

    kw = set(scope.get("keywords") or [])
    if kw:
        if keywords_present is None:
            return False
        present = {str(x).lower() for x in keywords_present}
        if not (kw & present):
            return False

    return True


# ── service ─────────────────────────────────────────────────────


class ComplianceLegalHoldsService:
    """DB-backed legal-hold operations."""

    async def list_holds(
        self,
        db: AsyncSession,
        *,
        status: Optional[str] = None,
        search: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[ComplianceHold]:
        q = select(ComplianceHold)
        if status:
            q = q.where(ComplianceHold.status == status)
        if search:
            s = f"%{search}%"
            q = q.where(or_(
                ComplianceHold.name.ilike(s),
                ComplianceHold.case_ref.ilike(s),
                ComplianceHold.description.ilike(s),
            ))
        q = q.order_by(desc(ComplianceHold.created_at)).offset(offset).limit(limit)
        return list((await db.execute(q)).scalars().all())

    async def get(self, db: AsyncSession, hold_id: str) -> Optional[ComplianceHold]:
        return (await db.execute(
            select(ComplianceHold).where(ComplianceHold.id == hold_id)
        )).scalar_one_or_none()

    async def create(
        self,
        db: AsyncSession,
        *,
        name: str,
        case_ref: Optional[str],
        scope: Dict[str, Any],
        retention_override: bool,
        notify: bool,
        expires_at: Optional[datetime],
        description: Optional[str],
        actor_id: str,
        check_conflicts: bool = True,
    ) -> Tuple[ComplianceHold, List[ComplianceHold]]:
        norm = _normalize_scope(scope)

        conflicts: List[ComplianceHold] = []
        if check_conflicts:
            actives = (await db.execute(
                select(ComplianceHold).where(ComplianceHold.status == "active")
            )).scalars().all()
            for h in actives:
                if _scope_overlaps(h.scope or {}, norm):
                    conflicts.append(h)

        hold = ComplianceHold(
            id=uuid.uuid4().hex,
            name=name,
            case_ref=case_ref,
            description=description,
            scope=norm,
            retention_override=bool(retention_override),
            notify=bool(notify),
            status="active",
            created_by=actor_id,
            expires_at=expires_at,
        )
        db.add(hold)
        await db.flush()

        db.add(ComplianceHoldAudit(
            id=uuid.uuid4().hex,
            hold_id=hold.id,
            event="hold.created",
            actor_id=actor_id,
            details={"name": name, "scope": norm, "conflicts": [c.id for c in conflicts]},
        ))
        await db.commit()
        audit_log(
            "compliance.hold_created",
            user_id=actor_id, success=True,
            details={"hold_id": hold.id, "name": name, "conflicts": [c.id for c in conflicts]},
        )
        return hold, conflicts

    async def update_scope(
        self,
        db: AsyncSession,
        *,
        hold_id: str,
        scope: Dict[str, Any],
        actor_id: str,
    ) -> ComplianceHold:
        h = await self.get(db, hold_id)
        if h is None:
            raise LookupError(hold_id)
        if h.status != "active":
            raise ValueError("hold not active")
        old_scope = h.scope
        h.scope = _normalize_scope(scope)
        db.add(ComplianceHoldAudit(
            id=uuid.uuid4().hex,
            hold_id=h.id,
            event="hold.scope_updated",
            actor_id=actor_id,
            details={"old": old_scope, "new": h.scope},
        ))
        await db.commit()
        audit_log(
            "compliance.hold_scope_updated",
            user_id=actor_id, success=True,
            details={"hold_id": h.id},
        )
        return h

    async def release(
        self,
        db: AsyncSession,
        *,
        hold_id: str,
        reason: str,
        actor_id: str,
    ) -> ComplianceHold:
        h = await self.get(db, hold_id)
        if h is None:
            raise LookupError(hold_id)
        if h.status != "active":
            raise ValueError("hold not active")
        h.status = "released"
        h.released_at = _now()
        h.released_by = actor_id
        h.release_reason = reason
        db.add(ComplianceHoldAudit(
            id=uuid.uuid4().hex,
            hold_id=h.id,
            event="hold.released",
            actor_id=actor_id,
            details={"reason": reason},
        ))
        await db.commit()
        audit_log(
            "compliance.hold_released",
            user_id=actor_id, success=True,
            details={"hold_id": h.id, "reason": reason},
        )
        return h

    async def audit_trail(
        self, db: AsyncSession, hold_id: str, *, limit: int = 500,
    ) -> List[ComplianceHoldAudit]:
        rows = (await db.execute(
            select(ComplianceHoldAudit)
            .where(ComplianceHoldAudit.hold_id == hold_id)
            .order_by(desc(ComplianceHoldAudit.occurred_at))
            .limit(limit)
        )).scalars().all()
        return list(rows)

    # ── lookup API used by retention / RTBF ─────────────────

    async def is_under_hold(
        self,
        *,
        resource_type: str,
        resource_id: Optional[str] = None,
        subject_id: Optional[str] = None,
        timestamp: Optional[datetime] = None,
        channel_id: Optional[str] = None,
        file_type: Optional[str] = None,
        message_type: Optional[str] = None,
        db: Optional[AsyncSession] = None,
    ) -> Tuple[bool, List[str]]:
        """Returns (under_hold, [hold_ids that match])."""
        ids: List[str] = []
        rows = await self._active_holds(db)
        for h in rows:
            if _hold_matches(
                h,
                resource_type=resource_type,
                resource_id=resource_id or "",
                subject_id=subject_id,
                timestamp=timestamp,
                channel_id=channel_id,
                file_type=file_type,
                message_type=message_type,
            ):
                ids.append(h.id)
        return (bool(ids), ids)

    async def find_subject_holds(
        self,
        subject_id: str,
        *,
        db: Optional[AsyncSession] = None,
    ) -> List[ComplianceHold]:
        rows = await self._active_holds(db)
        out: List[ComplianceHold] = []
        for h in rows:
            cust = set((h.scope or {}).get("custodians") or [])
            if not cust or subject_id in cust:
                out.append(h)
        return out

    async def _active_holds(
        self, db: Optional[AsyncSession],
    ) -> List[ComplianceHold]:
        if db is not None:
            rows = (await db.execute(
                select(ComplianceHold).where(ComplianceHold.status == "active")
            )).scalars().all()
            return list(rows)
        async with async_session_factory() as session:
            rows = (await session.execute(
                select(ComplianceHold).where(ComplianceHold.status == "active")
            )).scalars().all()
            return list(rows)


# Singleton convenience
legal_holds_service = ComplianceLegalHoldsService()
