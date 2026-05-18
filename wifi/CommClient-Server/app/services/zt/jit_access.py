"""
Zero-Trust — just-in-time elevated access.

* Users request elevated scope on a resource.
* Another admin approves.
* Grant becomes active; expires automatically.
* Auto-revoke when expired; full audit trail.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.session import async_session_factory
from app.models.zt import JITGrant

logger = get_logger(__name__)


DEFAULT_TTL_HOURS = 1
MAX_TTL_HOURS = 8
SWEEP_INTERVAL = 60.0


class JITAccess:
    """JIT grant lifecycle."""

    def __init__(self) -> None:
        self._task: Optional[asyncio.Task[None]] = None
        self._stop = asyncio.Event()

    async def request_grant(
        self,
        *,
        user_id: str,
        resource: str,
        scopes: list[str],
        reason: str,
        ttl_hours: int = DEFAULT_TTL_HOURS,
    ) -> JITGrant:
        ttl_hours = max(1, min(MAX_TTL_HOURS, ttl_hours))
        now = datetime.now(timezone.utc)
        grant = JITGrant(
            user_id=user_id,
            resource=resource,
            scopes=list(scopes),
            reason=reason,
            granted_by=None,
            granted_at=None,
            expires_at=now + timedelta(hours=ttl_hours),
            status="pending",
        )
        async with async_session_factory() as db:
            db.add(grant)
            await db.commit()
            await db.refresh(grant)
        return grant

    async def approve(
        self, grant_id: str, approver_id: str,
    ) -> Optional[JITGrant]:
        async with async_session_factory() as db:
            row = (await db.execute(
                select(JITGrant).where(JITGrant.id == grant_id)
            )).scalar_one_or_none()
            if row is None or row.status != "pending":
                return None
            if row.user_id == approver_id:
                # No self-approval.
                return None
            row.granted_by = approver_id
            row.granted_at = datetime.now(timezone.utc)
            row.status = "active"
            await db.commit()
            await db.refresh(row)
        return row

    async def revoke(self, grant_id: str) -> bool:
        async with async_session_factory() as db:
            row = (await db.execute(
                select(JITGrant).where(JITGrant.id == grant_id)
            )).scalar_one_or_none()
            if row is None:
                return False
            row.status = "revoked"
            row.revoked_at = datetime.now(timezone.utc)
            await db.commit()
        return True

    async def is_active_for(
        self, user_id: str, resource: str, scope: str,
    ) -> bool:
        async with async_session_factory() as db:
            r = await db.execute(
                select(JITGrant).where(
                    JITGrant.user_id == user_id,
                    JITGrant.status == "active",
                    JITGrant.resource == resource,
                )
            )
            rows = r.scalars().all()
        now = datetime.now(timezone.utc)
        for row in rows:
            if row.expires_at < now:
                continue
            if scope in (row.scopes or []) or "*" in (row.scopes or []):
                return True
        return False

    async def list_pending(self) -> list[JITGrant]:
        async with async_session_factory() as db:
            r = await db.execute(
                select(JITGrant).where(JITGrant.status == "pending")
                .order_by(JITGrant.created_at)
            )
            return list(r.scalars().all())

    async def list_active(self) -> list[JITGrant]:
        async with async_session_factory() as db:
            r = await db.execute(
                select(JITGrant).where(JITGrant.status == "active")
            )
            return list(r.scalars().all())

    # ── auto-expire loop ────────────────────────────────────

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._sweep_loop(), name="zt-jit-sweep")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _sweep_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._sweep_once()
            except Exception as exc:
                logger.warning("zt_jit_sweep_err err=%s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=SWEEP_INTERVAL)
            except asyncio.TimeoutError:
                continue

    async def _sweep_once(self) -> int:
        now = datetime.now(timezone.utc)
        async with async_session_factory() as db:
            r = await db.execute(
                select(JITGrant).where(
                    JITGrant.status == "active",
                    JITGrant.expires_at < now,
                )
            )
            rows = list(r.scalars().all())
            n = 0
            for row in rows:
                row.status = "expired"
                n += 1
            if n:
                await db.commit()
        return n


_jit: Optional[JITAccess] = None


def get_jit_access() -> JITAccess:
    global _jit
    if _jit is None:
        _jit = JITAccess()
    return _jit
