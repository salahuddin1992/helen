"""
Phase 6 / Module AE — Lightweight intrusion detection.

Watches the live event stream + DB-backed login attempts and applies a
simple heuristic ladder per source IP:

* failed_login_rate > N in last 5m       → log
* failed_login_rate > 2N + 4xx burst     → throttle (temp 5m)
* >X distinct 4xx endpoints in 10m       → temp block 1h
* persistent abuse                       → permanent ban (persisted)

Auto-unban runs every minute; everything is async-safe.
"""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.session import async_session_factory
from app.models.security import IPBlock, LoginAttempt, SecurityEvent
from app.observability.metrics_exporter import counter_inc

logger = get_logger(__name__)


@dataclass
class IDSConfig:
    # Sliding-window thresholds.
    failed_logins_log: int = 5            # 5m
    failed_logins_throttle: int = 15      # 5m
    err_endpoints_block: int = 25         # 10m
    block_duration_minutes: int = 60
    throttle_duration_minutes: int = 5
    ban_after_blocks: int = 3             # this many temp blocks ⇒ perma-ban


@dataclass
class _IPState:
    failed_logins: deque[float] = field(default_factory=deque)
    err_endpoints: dict[str, float] = field(default_factory=dict)
    temp_blocks: int = 0
    throttled_until: float = 0.0
    last_action_ts: float = 0.0


class IntrusionDetector:
    """Pluggable async detector. Persists IP blocks in DB."""

    def __init__(self, cfg: Optional[IDSConfig] = None) -> None:
        self.cfg = cfg or IDSConfig()
        self._state: dict[str, _IPState] = defaultdict(_IPState)
        self._lock = asyncio.Lock()
        self._task: Optional[asyncio.Task[None]] = None
        self._stop = asyncio.Event()

    # ── lifecycle ───────────────────────────────────────────

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._unban_loop(), name="ids-unban")
        logger.info("ids: started")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=2.0)
            except asyncio.TimeoutError:                            # pragma: no cover
                self._task.cancel()

    # ── live signal entry points ────────────────────────────

    async def record_login(self, ip: str, username: str, success: bool,
                           user_agent: Optional[str] = None) -> None:
        async with async_session_factory() as db:
            db.add(LoginAttempt(
                username=username, ip=ip,
                user_agent=user_agent, success=bool(success),
            ))
            try:
                await db.commit()
            except Exception:                                       # pragma: no cover
                await db.rollback()
        if success:
            return
        await self._tick_failed_login(ip)

    async def record_http_4xx(self, ip: str, endpoint: str, status: int) -> None:
        async with self._lock:
            st = self._state[ip]
            st.err_endpoints[endpoint] = time.time()
            # prune older than 10m
            now = time.time()
            for k in list(st.err_endpoints):
                if now - st.err_endpoints[k] > 600:
                    st.err_endpoints.pop(k, None)
            if len(st.err_endpoints) >= self.cfg.err_endpoints_block:
                await self._temp_block(ip, "endpoint enumeration")

    async def is_blocked(self, ip: str) -> bool:
        async with self._lock:
            st = self._state.get(ip)
            if st and st.throttled_until > time.time():
                return True
        # also consult DB
        async with async_session_factory() as db:
            row = (await db.execute(
                select(IPBlock).where(IPBlock.ip_cidr == ip)
            )).scalar_one_or_none()
            if row is None:
                return False
            if row.expires_at is None:
                return True
            return row.expires_at > datetime.now(timezone.utc)

    # ── thresholds ──────────────────────────────────────────

    async def _tick_failed_login(self, ip: str) -> None:
        async with self._lock:
            st = self._state[ip]
            now = time.time()
            st.failed_logins.append(now)
            while st.failed_logins and now - st.failed_logins[0] > 300:
                st.failed_logins.popleft()
            n = len(st.failed_logins)
        if n >= self.cfg.failed_logins_throttle:
            await self._throttle(ip, "failed-login burst")
        elif n >= self.cfg.failed_logins_log:
            await self._log_event(ip, "ids_alert", "warning",
                                  {"reason": "failed-login rate", "count": n})

    async def _throttle(self, ip: str, reason: str) -> None:
        async with self._lock:
            st = self._state[ip]
            now = time.time()
            st.throttled_until = max(st.throttled_until,
                                     now + self.cfg.throttle_duration_minutes * 60)
            st.last_action_ts = now
        await self._log_event(ip, "ids_alert", "warning",
                              {"action": "throttle", "reason": reason})
        counter_inc("ids_events_total", kind="failed_login", action="throttle")

    async def _temp_block(self, ip: str, reason: str) -> None:
        await self._log_event(ip, "ip_blocked", "high",
                              {"reason": reason, "duration_min":
                               self.cfg.block_duration_minutes})
        expires = datetime.now(timezone.utc) + timedelta(
            minutes=self.cfg.block_duration_minutes,
        )
        async with async_session_factory() as db:
            db.add(IPBlock(
                ip_cidr=ip, reason=reason,
                expires_at=expires, blocked_by="ids",
            ))
            try:
                await db.commit()
            except Exception:                                       # pragma: no cover
                await db.rollback()
        async with self._lock:
            st = self._state[ip]
            st.temp_blocks += 1
            if st.temp_blocks >= self.cfg.ban_after_blocks:
                await self._perma_ban(ip, "repeated temp blocks")
        counter_inc("ids_events_total", kind="endpoint_enum", action="block")

    async def _perma_ban(self, ip: str, reason: str) -> None:
        async with async_session_factory() as db:
            db.add(IPBlock(
                ip_cidr=ip, reason=reason, expires_at=None,
                blocked_by="ids",
            ))
            try:
                await db.commit()
            except Exception:                                       # pragma: no cover
                await db.rollback()
        await self._log_event(ip, "ip_blocked", "critical",
                              {"action": "perma_ban", "reason": reason})
        counter_inc("ids_events_total", kind="perma_ban", action="ban")

    async def _log_event(self, ip: str, kind: str, severity: str,
                         payload: dict) -> None:
        try:
            async with async_session_factory() as db:
                db.add(SecurityEvent(
                    kind=kind, severity=severity, ip=ip,
                    payload=payload,
                ))
                await db.commit()
        except Exception:                                           # pragma: no cover
            pass
        logger.warning("IDS %s/%s ip=%s payload=%s", kind, severity, ip, payload)

    async def _unban_loop(self) -> None:
        while not self._stop.is_set():
            try:
                cutoff = datetime.now(timezone.utc)
                async with async_session_factory() as db:
                    res = await db.execute(
                        select(IPBlock).where(IPBlock.expires_at != None)  # noqa: E711
                                       .where(IPBlock.expires_at <= cutoff)
                    )
                    for row in res.scalars().all():
                        await db.delete(row)
                        await self._log_event(row.ip_cidr, "ip_unblocked",
                                              "info", {"auto": True})
                    await db.commit()
            except Exception as exc:                                # pragma: no cover
                logger.warning("ids: unban loop err (%s)", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=60.0)
            except asyncio.TimeoutError:
                continue


_singleton: Optional[IntrusionDetector] = None


def get_ids() -> IntrusionDetector:
    global _singleton
    if _singleton is None:
        _singleton = IntrusionDetector()
    return _singleton
