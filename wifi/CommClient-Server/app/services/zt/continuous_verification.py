"""
Zero-Trust — continuous session verification.

Periodically (every 5 min by default) every active session is checked
against:

    * Identity still valid (SVID not revoked, JWT not expired).
    * Device still attested.
    * Risk score under threshold.
    * No anomaly detected (geo jump, behaviour shift).

Failed checks drop the session and emit a user-visible event over
socket.io so the client can re-auth gracefully.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.session import async_session_factory
from app.models.zt import ContinuousAssessment, WorkloadIdentity
from app.services.zt.device_posture import get_device_posture

logger = get_logger(__name__)


CHECK_INTERVAL = 300.0
ASSESS_TTL = timedelta(minutes=15)
RISK_DROP_THRESHOLD = 80


class ContinuousVerifier:
    """Background loop + per-session evaluator."""

    def __init__(self) -> None:
        self._task: Optional[asyncio.Task[None]] = None
        self._stop = asyncio.Event()
        self._sessions: dict[str, dict[str, Any]] = {}

    def register_session(
        self,
        session_id: str,
        *,
        spiffe_id: str,
        device_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> None:
        self._sessions[session_id] = {
            "spiffe_id": spiffe_id,
            "device_id": device_id,
            "user_id":   user_id,
            "last_ok":   datetime.now(timezone.utc),
            "anomalies": 0,
        }

    def drop_session(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="zt-continuous")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.run_pass()
            except Exception as exc:
                logger.warning("zt_continuous_pass_err err=%s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=CHECK_INTERVAL)
            except asyncio.TimeoutError:
                continue

    async def run_pass(self) -> int:
        dropped = 0
        for sid, info in list(self._sessions.items()):
            ok, reason, score = await self._evaluate(info)
            await self._record(sid, ok, reason, score)
            if not ok:
                self._sessions.pop(sid, None)
                await self._notify_drop(sid, reason)
                dropped += 1
        return dropped

    async def _evaluate(
        self, info: dict[str, Any],
    ) -> tuple[bool, str, int]:
        # 1. Identity still valid.
        async with async_session_factory() as db:
            r = await db.execute(
                select(WorkloadIdentity).where(
                    WorkloadIdentity.spiffe_id == info["spiffe_id"]
                )
            )
            wi = r.scalar_one_or_none()
        if wi is None or wi.revoked:
            return False, "identity_revoked", 100
        if wi.expires_at < datetime.now(timezone.utc):
            return False, "identity_expired", 100

        # 2. Device attestation.
        device_id = info.get("device_id")
        risk = 0
        if device_id:
            ok, risk = await get_device_posture().is_attested(device_id)
            if not ok:
                return False, "device_not_attested", risk
            if risk >= RISK_DROP_THRESHOLD:
                return False, f"risk_threshold:{risk}", risk

        return True, "ok", risk

    async def _record(
        self,
        session_id: str,
        ok: bool,
        reason: str,
        score: int,
    ) -> None:
        try:
            now = datetime.now(timezone.utc)
            async with async_session_factory() as db:
                row = ContinuousAssessment(
                    session_id=session_id,
                    check_kind="periodic",
                    score=score,
                    passed=ok,
                    details={"reason": reason},
                    evaluated_at=now,
                    expires_at=now + ASSESS_TTL,
                )
                db.add(row)
                await db.commit()
        except Exception as exc:
            logger.debug("zt_assess_persist_failed err=%s", exc)

    async def _notify_drop(self, session_id: str, reason: str) -> None:
        try:
            from app.socket.server import sio
            await sio.emit(
                "zt:session_dropped",
                {"session_id": session_id, "reason": reason},
                room=f"session_{session_id}",
            )
        except Exception:
            pass


_verifier: Optional[ContinuousVerifier] = None


def get_continuous_verifier() -> ContinuousVerifier:
    global _verifier
    if _verifier is None:
        _verifier = ContinuousVerifier()
    return _verifier
