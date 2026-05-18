"""
Legal Hold service.

Legal holds suspend retention/deletion for a defined scope of audit
data (and related resources) for the duration of an investigation,
litigation, or regulatory inquiry. While a hold is active, every
retention policy must skip resources whose attributes match the
hold's scope.

Scope fields (all optional, AND-combined):
    actors:        list[str]   exact actor IDs
    channels:      list[str]   channel/resource IDs
    resources:     list[str]   generic resource IDs
    keywords:      list[str]   substring match on action / payload
    file_types:    list[str]   payload.file_type values
    severity_min:  str         minimum severity (info|low|...|critical)
    from_ts/to_ts: float       timestamp range (inclusive)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from sqlalchemy import select, or_, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_log
from app.core.logging import get_logger
from app.db.session import async_session_factory
from app.models.legal_hold import LegalHold

logger = get_logger(__name__)

SEVERITY_RANK = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}


class LegalHoldConflict(ValueError):
    """Raised when a new hold scope overlaps with an existing one."""


def _scopes_overlap(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """A conservative overlap detection: if both holds list the *same*
    actor / channel / resource OR neither restricts that dimension and
    timestamp windows touch."""
    for key in ("actors", "channels", "resources"):
        av = set(a.get(key) or [])
        bv = set(b.get(key) or [])
        if av and bv and not (av & bv):
            # Both restricted but disjoint — no overlap on this axis
            return False
    # If we reach here, every restricted axis either matched or one side
    # was wildcard. Check time-window overlap.
    a_from = float(a.get("from_ts") or 0)
    a_to = float(a.get("to_ts") or 1e18)
    b_from = float(b.get("from_ts") or 0)
    b_to = float(b.get("to_ts") or 1e18)
    return not (a_to < b_from or b_to < a_from)


class LegalHoldService:
    """CRUD + conflict detection + scope matching for legal holds."""

    async def list(
        self, status: Optional[str] = None, limit: int = 200,
    ) -> list[dict[str, Any]]:
        async with async_session_factory() as db:
            stmt = select(LegalHold).order_by(LegalHold.created_at.desc())
            if status:
                stmt = stmt.where(LegalHold.status == status)
            stmt = stmt.limit(limit)
            res = await db.execute(stmt)
            rows = res.scalars().all()
            return [r.to_dict() for r in rows]

    async def get(self, hold_id: str) -> Optional[dict[str, Any]]:
        async with async_session_factory() as db:
            res = await db.execute(
                select(LegalHold).where(LegalHold.id == hold_id)
            )
            r = res.scalar_one_or_none()
            return r.to_dict() if r else None

    async def create(
        self,
        *,
        name: str,
        scope: dict[str, Any],
        actor_id: str,
        case_ref: Optional[str] = None,
        description: Optional[str] = None,
        ends_at: Optional[datetime] = None,
        force: bool = False,
    ) -> dict[str, Any]:
        # Conflict detection — fail unless force=True
        async with async_session_factory() as db:
            existing = await db.execute(
                select(LegalHold).where(LegalHold.status == "active")
            )
            for h in existing.scalars().all():
                if _scopes_overlap(scope, h.scope or {}):
                    if not force:
                        raise LegalHoldConflict(
                            f"hold scope overlaps existing hold {h.name!r}"
                        )
                    logger.warning(
                        "legal_hold_overlap_forced",
                        new_name=name, existing_id=h.id,
                    )

            row = LegalHold(
                name=name,
                case_ref=case_ref,
                description=description,
                scope=dict(scope or {}),
                status="active",
                ends_at=ends_at,
                created_by=actor_id,
            )
            db.add(row)
            await db.commit()
            await db.refresh(row)
            result = row.to_dict()

        audit_log("siem.legal_hold.create",
                  user_id=actor_id, success=True,
                  details={"hold_id": result["id"], "name": name})
        return result

    async def update(
        self, hold_id: str, *, actor_id: str, **fields: Any,
    ) -> Optional[dict[str, Any]]:
        async with async_session_factory() as db:
            res = await db.execute(
                select(LegalHold).where(LegalHold.id == hold_id)
            )
            row = res.scalar_one_or_none()
            if not row:
                return None
            for k, v in fields.items():
                if hasattr(row, k) and v is not None:
                    setattr(row, k, v)
            await db.commit()
            await db.refresh(row)
            result = row.to_dict()

        audit_log("siem.legal_hold.update",
                  user_id=actor_id, success=True,
                  details={"hold_id": hold_id, "fields": list(fields.keys())})
        return result

    async def release(
        self, hold_id: str, *, actor_id: str, reason: str,
        confirmation: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Release a hold. Requires ``confirmation == name`` of the hold
        to guard against accidental release."""
        async with async_session_factory() as db:
            res = await db.execute(
                select(LegalHold).where(LegalHold.id == hold_id)
            )
            row = res.scalar_one_or_none()
            if not row:
                return None
            if confirmation is not None and confirmation != row.name:
                audit_log("siem.legal_hold.release",
                          user_id=actor_id, success=False,
                          details={"hold_id": hold_id, "reason": "bad_confirmation"})
                raise ValueError("confirmation must match hold name")
            row.status = "released"
            row.released_at = datetime.now(timezone.utc)
            row.released_by = actor_id
            row.release_reason = reason
            await db.commit()
            await db.refresh(row)
            result = row.to_dict()

        audit_log("siem.legal_hold.release",
                  user_id=actor_id, success=True,
                  details={"hold_id": hold_id, "reason": reason})
        return result

    async def active_holds(self) -> list[dict[str, Any]]:
        return await self.list(status="active", limit=1000)

    async def is_under_hold(
        self,
        *,
        resource_type: str,
        resource_id: Optional[str] = None,
        actor: Optional[str] = None,
        timestamp: Optional[float] = None,
        severity: Optional[str] = None,
        payload: Optional[dict[str, Any]] = None,
    ) -> bool:
        """Returns True if the (resource, actor, timestamp) tuple
        matches at least one active hold's scope. Used by retention."""
        holds = await self.active_holds()
        for h in holds:
            scope = h.get("scope") or {}
            if not self._scope_matches(
                scope, resource_type=resource_type,
                resource_id=resource_id, actor=actor,
                timestamp=timestamp, severity=severity,
                payload=payload,
            ):
                continue
            return True
        return False

    @staticmethod
    def _scope_matches(
        scope: dict[str, Any],
        *,
        resource_type: str,
        resource_id: Optional[str],
        actor: Optional[str],
        timestamp: Optional[float],
        severity: Optional[str],
        payload: Optional[dict[str, Any]],
    ) -> bool:
        if scope.get("actors") and actor not in scope["actors"]:
            return False
        if scope.get("channels") and resource_id not in scope["channels"]:
            return False
        if scope.get("resources") and resource_id not in scope["resources"]:
            return False
        if scope.get("severity_min") and severity:
            need = SEVERITY_RANK.get(scope["severity_min"].lower(), 0)
            have = SEVERITY_RANK.get(severity.lower(), 0)
            if have < need:
                return False
        if scope.get("file_types") and payload:
            ft = payload.get("file_type")
            if ft not in scope["file_types"]:
                return False
        if scope.get("keywords"):
            blob = " ".join(filter(None, [
                resource_type, resource_id or "", actor or "",
                str(payload or {}),
            ])).lower()
            if not any(kw.lower() in blob for kw in scope["keywords"]):
                return False
        if scope.get("from_ts") is not None and timestamp is not None:
            if timestamp < float(scope["from_ts"]):
                return False
        if scope.get("to_ts") is not None and timestamp is not None:
            if timestamp > float(scope["to_ts"]):
                return False
        return True


_service: Optional[LegalHoldService] = None


def get_legal_hold_service() -> LegalHoldService:
    global _service
    if _service is None:
        _service = LegalHoldService()
    return _service


__all__ = [
    "LegalHoldService",
    "LegalHoldConflict",
    "get_legal_hold_service",
]
