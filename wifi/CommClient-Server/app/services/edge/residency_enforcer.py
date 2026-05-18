"""
Edge — data residency enforcer.

Per-workspace data-residency policy. If a workspace requires data to
stay in ``eu-west-1``, any attempted edge-route or replication into a
non-EU node is blocked.

Enforcement points:
    * ``check_route(workspace_id, edge_node)`` — before steering traffic
    * ``check_replication(workspace_id, target_region)`` — before async
      replication
    * audit log row on every cross-region attempt
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.session import async_session_factory
from app.models.edge import EdgeNode, EdgeRegion, RegionPolicy

logger = get_logger(__name__)


@dataclass
class ResidencyDecision:
    allowed: bool
    reason: str = ""
    required_region: Optional[str] = None


class ResidencyEnforcer:
    """Stateless enforcer (DB-backed)."""

    async def check_route(
        self,
        workspace_id: Optional[str],
        edge_node: EdgeNode,
        *,
        db: Optional[AsyncSession] = None,
    ) -> ResidencyDecision:
        if not workspace_id:
            return ResidencyDecision(allowed=True, reason="no_workspace")

        async def _check(db: AsyncSession) -> ResidencyDecision:
            r = await db.execute(
                select(RegionPolicy).where(RegionPolicy.workspace_id == workspace_id)
            )
            pol = r.scalar_one_or_none()
            if pol is None:
                return ResidencyDecision(allowed=True, reason="no_policy")
            required = pol.required_residency_region
            allowed_regions = list(pol.allowed_regions or [])
            if required and edge_node.region != required:
                await self._audit(
                    db,
                    workspace_id=workspace_id,
                    edge_region=edge_node.region,
                    required=required,
                    allowed=False,
                    reason="residency_mismatch",
                )
                return ResidencyDecision(
                    allowed=False,
                    reason="residency_required",
                    required_region=required,
                )
            if allowed_regions and edge_node.region not in allowed_regions:
                await self._audit(
                    db,
                    workspace_id=workspace_id,
                    edge_region=edge_node.region,
                    required=",".join(allowed_regions),
                    allowed=False,
                    reason="region_not_allowed",
                )
                return ResidencyDecision(
                    allowed=False,
                    reason="region_not_allowed",
                )
            return ResidencyDecision(allowed=True)

        if db is None:
            async with async_session_factory() as _db:
                return await _check(_db)
        return await _check(db)

    async def check_replication(
        self,
        workspace_id: Optional[str],
        target_region: str,
    ) -> ResidencyDecision:
        if not workspace_id:
            return ResidencyDecision(allowed=True)
        async with async_session_factory() as db:
            r = await db.execute(
                select(RegionPolicy).where(RegionPolicy.workspace_id == workspace_id)
            )
            pol = r.scalar_one_or_none()
            if pol is None:
                return ResidencyDecision(allowed=True)
            allowed = list(pol.allowed_regions or [])
            required = pol.required_residency_region
            if required and target_region != required:
                return ResidencyDecision(
                    allowed=False, reason="residency_required",
                    required_region=required,
                )
            if allowed and target_region not in allowed:
                return ResidencyDecision(
                    allowed=False, reason="region_not_allowed",
                )
            return ResidencyDecision(allowed=True)

    async def _audit(
        self,
        db: AsyncSession,
        *,
        workspace_id: str,
        edge_region: str,
        required: str,
        allowed: bool,
        reason: str,
    ) -> None:
        # Best-effort audit into Security events table if available.
        try:
            from app.models.security import SecurityEvent
            from app.db.base import generate_uuid
            ev = SecurityEvent(
                id=generate_uuid(),
                kind="edge.residency",
                severity="warning" if not allowed else "info",
                payload={
                    "workspace_id": workspace_id,
                    "edge_region":  edge_region,
                    "required":     required,
                    "allowed":      allowed,
                    "reason":       reason,
                    "ts":           time.time(),
                },
            )
            db.add(ev)
            await db.commit()
        except Exception as exc:
            logger.debug("residency_audit_failed err=%s", exc)


_enforcer: Optional[ResidencyEnforcer] = None


def get_residency_enforcer() -> ResidencyEnforcer:
    global _enforcer
    if _enforcer is None:
        _enforcer = ResidencyEnforcer()
    return _enforcer
