"""
Module L — Agent lifecycle manager.

Responsibilities:
    * Registration → mint refresh + first access token
    * Refresh-token rotation
    * Heartbeat ingestion (deduplicated snapshot storage + presence flip)
    * Stale-agent reaper (background task, fires every 30 s)
    * Token revocation / agent soft-delete
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.session import async_session_factory
from app.models.agent import Agent, AgentEvent

logger = get_logger(__name__)


# ── Token helpers ───────────────────────────────────────────


def _agent_secret() -> bytes:
    """Server-side HMAC secret used to derive deterministic refresh tokens.

    Falls back to the JWT secret to avoid extra configuration. Refresh tokens
    are never reversible from the hash; the secret only binds tokens to this
    server instance so that swapped databases do not accidentally accept
    foreign tokens.
    """
    try:
        from app.core.config import get_settings  # local import to avoid cycle
        return get_settings().JWT_SECRET.encode()
    except Exception:
        return os.environ.get(
            "HELEN_AGENT_SECRET", "helen-agent-default-secret"
        ).encode()


def hash_refresh_token(token: str) -> str:
    return hmac.new(_agent_secret(), token.encode("utf-8"), hashlib.sha256).hexdigest()


def generate_refresh_token() -> str:
    # 256-bit URL-safe token (43 base64url chars without padding).
    return secrets.token_urlsafe(32)


# ── Data shapes ─────────────────────────────────────────────


@dataclass(slots=True)
class RegistrationResult:
    agent: Agent
    refresh_token: str


# ── Manager ─────────────────────────────────────────────────


class AgentManager:
    """Stateless service object — one shared instance per process."""

    STALE_THRESHOLD_SECONDS = 120
    REAPER_INTERVAL_SECONDS = 30

    def __init__(self) -> None:
        self._reaper_task: asyncio.Task[None] | None = None

    # ── Registration ────────────────────────────────────────

    async def register_agent(
        self,
        db: AsyncSession,
        *,
        fingerprint: str,
        hostname: str,
        os_name: str | None,
        os_version: str | None,
        agent_version: str | None,
        ip: str | None,
    ) -> RegistrationResult:
        existing = (
            await db.execute(select(Agent).where(Agent.fingerprint == fingerprint))
        ).scalar_one_or_none()

        refresh = generate_refresh_token()
        refresh_hash = hash_refresh_token(refresh)
        now = datetime.now(timezone.utc)

        if existing:
            existing.hostname = hostname or existing.hostname
            existing.os_name = os_name or existing.os_name
            existing.os_version = os_version or existing.os_version
            existing.agent_version = agent_version or existing.agent_version
            existing.refresh_token_hash = refresh_hash
            existing.refresh_token_issued_at = now
            existing.refresh_token_version = (existing.refresh_token_version or 1) + 1
            existing.last_ip = ip
            existing.is_active = True
            existing.status = "offline"
            agent = existing
            evt = AgentEvent(
                agent_id=agent.id,
                event_type="re_registered",
                payload_json=json.dumps({"ip": ip, "version": agent_version}),
            )
        else:
            agent = Agent(
                fingerprint=fingerprint,
                hostname=hostname,
                os_name=os_name,
                os_version=os_version,
                agent_version=agent_version,
                registered_at=now,
                status="offline",
                refresh_token_hash=refresh_hash,
                refresh_token_issued_at=now,
                refresh_token_version=1,
                last_ip=ip,
            )
            db.add(agent)
            await db.flush()
            evt = AgentEvent(
                agent_id=agent.id,
                event_type="registered",
                payload_json=json.dumps({"ip": ip, "version": agent_version}),
            )
        db.add(evt)
        await db.commit()
        await db.refresh(agent)
        logger.info("agent_registered", agent_id=agent.id, fingerprint=fingerprint, ip=ip)
        return RegistrationResult(agent=agent, refresh_token=refresh)

    # ── Refresh-token rotation ───────────────────────────────

    async def rotate_token(self, db: AsyncSession, agent_id: str) -> str:
        agent = (
            await db.execute(select(Agent).where(Agent.id == agent_id))
        ).scalar_one_or_none()
        if not agent:
            raise ValueError("unknown agent")
        new_token = generate_refresh_token()
        agent.refresh_token_hash = hash_refresh_token(new_token)
        agent.refresh_token_issued_at = datetime.now(timezone.utc)
        agent.refresh_token_version = (agent.refresh_token_version or 1) + 1
        db.add(AgentEvent(
            agent_id=agent.id,
            event_type="token_rotated",
            payload_json=json.dumps({"version": agent.refresh_token_version}),
        ))
        await db.commit()
        logger.info("agent_token_rotated", agent_id=agent_id)
        return new_token

    async def verify_refresh(
        self, db: AsyncSession, *, agent_id: str | None, refresh_token: str
    ) -> Agent | None:
        if agent_id:
            agent = (
                await db.execute(select(Agent).where(Agent.id == agent_id))
            ).scalar_one_or_none()
        else:
            # token-only lookup (slow path)
            target_hash = hash_refresh_token(refresh_token)
            agent = (
                await db.execute(
                    select(Agent).where(Agent.refresh_token_hash == target_hash)
                )
            ).scalar_one_or_none()
        if not agent or not agent.is_active:
            return None
        expected = agent.refresh_token_hash or ""
        if not hmac.compare_digest(expected, hash_refresh_token(refresh_token)):
            return None
        return agent

    # ── Heartbeat ───────────────────────────────────────────

    async def record_heartbeat(
        self,
        db: AsyncSession,
        agent_id: str,
        snapshot: dict[str, Any],
        ip: str | None,
    ) -> Agent:
        agent = (
            await db.execute(select(Agent).where(Agent.id == agent_id))
        ).scalar_one_or_none()
        if not agent or not agent.is_active:
            raise ValueError("unknown agent")
        agent.last_heartbeat_at = datetime.now(timezone.utc)
        prev_status = agent.status
        agent.status = "online"
        agent.last_snapshot_json = json.dumps(snapshot)
        if ip:
            agent.last_ip = ip
        db.add(AgentEvent(
            agent_id=agent.id,
            event_type="heartbeat",
            payload_json=None,  # snapshot lives on Agent itself
        ))
        if prev_status != "online":
            db.add(AgentEvent(
                agent_id=agent.id,
                event_type="online",
                payload_json=None,
            ))
        await db.commit()
        return agent

    # ── Stale reaper ────────────────────────────────────────

    async def mark_offline_stale(self, db: AsyncSession) -> int:
        threshold = datetime.now(timezone.utc) - timedelta(
            seconds=self.STALE_THRESHOLD_SECONDS
        )
        rows = (
            await db.execute(
                select(Agent).where(
                    Agent.status == "online",
                    Agent.last_heartbeat_at < threshold,
                )
            )
        ).scalars().all()
        for a in rows:
            a.status = "stale"
            db.add(AgentEvent(
                agent_id=a.id,
                event_type="stale",
                payload_json=json.dumps({
                    "last_heartbeat": a.last_heartbeat_at.isoformat() if a.last_heartbeat_at else None
                }),
            ))
        if rows:
            await db.commit()
            logger.info("agents_marked_stale", count=len(rows))
        return len(rows)

    async def revoke(self, db: AsyncSession, agent_id: str, reason: str) -> None:
        await db.execute(
            update(Agent)
            .where(Agent.id == agent_id)
            .values(
                is_active=False,
                status="revoked",
                refresh_token_hash=None,
            )
        )
        db.add(AgentEvent(
            agent_id=agent_id,
            event_type="revoked",
            payload_json=json.dumps({"reason": reason}),
        ))
        await db.commit()
        logger.info("agent_revoked", agent_id=agent_id, reason=reason)

    # ── Background reaper ───────────────────────────────────

    async def _reaper_loop(self) -> None:
        while True:
            try:
                async with async_session_factory() as db:
                    await self.mark_offline_stale(db)
            except Exception:
                logger.exception("agent_reaper_iteration_failed")
            await asyncio.sleep(self.REAPER_INTERVAL_SECONDS)

    def start_background(self) -> None:
        if self._reaper_task is None or self._reaper_task.done():
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            self._reaper_task = loop.create_task(self._reaper_loop())
            logger.info("agent_manager_reaper_started")

    def stop_background(self) -> None:
        if self._reaper_task and not self._reaper_task.done():
            self._reaper_task.cancel()
            self._reaper_task = None


# Singleton accessor
_manager_singleton: AgentManager | None = None


def get_agent_manager() -> AgentManager:
    global _manager_singleton
    if _manager_singleton is None:
        _manager_singleton = AgentManager()
    return _manager_singleton
