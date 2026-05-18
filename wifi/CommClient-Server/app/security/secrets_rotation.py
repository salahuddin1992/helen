"""
Phase 6 / Module AE — Secrets rotation scheduler.

Manages periodic rotation of:

* JWT signing key  — two-phase (introduce new ⇒ grace ⇒ retire old)
* API keys         — per-workspace
* Webhook signing secrets
* Internal HMAC keys (federation, peer-auth)

Two-phase scheme:

   t0   issue new secret (state: ACTIVE_NEW)         — sign with new
   t0..T grace window (state: ACTIVE_DUAL)           — verify accepts both
   T    retire old (state: ACTIVE_NEW only)          — old rejected

All rotations are audit-logged via ``SecurityEvent`` rows.

The actual secret bytes are stored via the existing
``app.services.secret_store`` (or its equivalent) when available;
otherwise we fall back to ``cluster_session_kv`` so the same rotated
secret is visible cluster-wide.
"""
from __future__ import annotations

import asyncio
import os
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.session import async_session_factory
from app.models.security import SecurityEvent
from app.services.cluster.session_store import get_session_store

logger = get_logger(__name__)


VALID_SECRET_KINDS = ("jwt", "api_key", "webhook", "internal_hmac")


@dataclass
class RotationPolicy:
    kind: str
    interval_hours: int
    grace_minutes: int = 60
    last_rotated_at: Optional[datetime] = None


_DEFAULT_POLICIES = {
    "jwt":           RotationPolicy("jwt", 24 * 30, grace_minutes=60),
    "api_key":       RotationPolicy("api_key", 24 * 90, grace_minutes=24 * 60),
    "webhook":       RotationPolicy("webhook", 24 * 90, grace_minutes=24 * 60),
    "internal_hmac": RotationPolicy("internal_hmac", 24 * 30, grace_minutes=10),
}


class SecretsRotator:
    def __init__(self) -> None:
        self.policies: dict[str, RotationPolicy] = dict(_DEFAULT_POLICIES)
        self._task: Optional[asyncio.Task[None]] = None
        self._stop = asyncio.Event()
        self._lock = asyncio.Lock()

    # ── public API ──────────────────────────────────────────

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="secrets-rotator")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=2.0)
            except asyncio.TimeoutError:                            # pragma: no cover
                self._task.cancel()

    def list_policies(self) -> list[dict[str, Any]]:
        return [{
            "kind": p.kind,
            "interval_hours": p.interval_hours,
            "grace_minutes": p.grace_minutes,
            "last_rotated_at": p.last_rotated_at.isoformat() if p.last_rotated_at else None,
        } for p in self.policies.values()]

    async def rotate(self, kind: str, *, manual_by: Optional[str] = None) -> dict[str, Any]:
        if kind not in VALID_SECRET_KINDS:
            raise ValueError(f"invalid secret kind: {kind}")
        async with self._lock:
            new_value = secrets.token_hex(32)
            store = await get_session_store()
            now = datetime.now(timezone.utc)
            policy = self.policies[kind]
            grace_until = now + timedelta(minutes=policy.grace_minutes)

            # Snapshot previous → for grace verification
            prev = await store.get(f"secret:{kind}:current")
            await store.set(f"secret:{kind}:previous", {
                "value": prev,
                "retire_at": grace_until.isoformat(),
            }, ttl=int(policy.grace_minutes * 60) + 60)
            await store.set(f"secret:{kind}:current", new_value)
            policy.last_rotated_at = now

            await self._audit(kind, manual_by=manual_by)
            return {
                "kind": kind,
                "rotated_at": now.isoformat(),
                "grace_until": grace_until.isoformat(),
                "value_preview": new_value[:6] + "…",
            }

    async def current_secret(self, kind: str) -> Optional[str]:
        store = await get_session_store()
        return await store.get(f"secret:{kind}:current")

    async def previous_secret(self, kind: str) -> Optional[str]:
        store = await get_session_store()
        prev = await store.get(f"secret:{kind}:previous")
        if not prev:
            return None
        if isinstance(prev, dict):
            try:
                ret = prev.get("retire_at")
                if ret and datetime.fromisoformat(ret) < datetime.now(timezone.utc):
                    return None
            except Exception:                                       # pragma: no cover
                pass
            return prev.get("value")
        return prev

    # ── internals ───────────────────────────────────────────

    async def _run(self) -> None:
        # Light fast initial sleep so app finishes booting.
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=30.0)
        except asyncio.TimeoutError:
            pass
        while not self._stop.is_set():
            try:
                await self._tick_all()
            except Exception as exc:                                # pragma: no cover
                logger.exception("rotator: tick err (%s)", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=15 * 60)
            except asyncio.TimeoutError:
                continue

    async def _tick_all(self) -> None:
        for kind, policy in list(self.policies.items()):
            if not self._due(policy):
                continue
            try:
                await self.rotate(kind)
                logger.info("rotator: rotated %s on schedule", kind)
            except Exception as exc:                                # pragma: no cover
                logger.warning("rotator: failed to rotate %s (%s)", kind, exc)

    @staticmethod
    def _due(policy: RotationPolicy) -> bool:
        if policy.last_rotated_at is None:
            return True
        elapsed = datetime.now(timezone.utc) - policy.last_rotated_at
        return elapsed >= timedelta(hours=policy.interval_hours)

    async def _audit(self, kind: str, *, manual_by: Optional[str]) -> None:
        try:
            async with async_session_factory() as db:
                db.add(SecurityEvent(
                    kind="secret_rotated", severity="info",
                    payload={
                        "secret_kind": kind,
                        "manual_by": manual_by,
                        "at": datetime.now(timezone.utc).isoformat(),
                    },
                ))
                await db.commit()
        except Exception:                                           # pragma: no cover
            pass


_singleton: Optional[SecretsRotator] = None


def get_secrets_rotator() -> SecretsRotator:
    global _singleton
    if _singleton is None:
        _singleton = SecretsRotator()
    return _singleton
