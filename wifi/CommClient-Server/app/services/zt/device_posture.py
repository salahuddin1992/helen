"""
Zero-Trust — device posture & attestation.

Clients submit periodic attestations via ``POST /api/zt/attest``. The
server validates the claims, calculates a 0..100 risk score, and
stores the result. Sessions tied to a stale or failed attestation are
forced to re-attest.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.session import async_session_factory
from app.models.zt import DeviceAttestation

logger = get_logger(__name__)


ATTESTATION_TTL = timedelta(hours=24)


# Minimum-supported versions per OS. Anything older bumps the risk score.
MIN_OS_VERSIONS = {
    "ios":     "16.0",
    "android": "13",
    "macos":   "13",
    "windows": "10.0",
    "linux":   "5.0",
}


def _version_lt(a: str, b: str) -> bool:
    def _parts(x: str) -> list[int]:
        out: list[int] = []
        for tok in x.split("."):
            try:
                out.append(int(tok))
            except Exception:
                out.append(0)
        return out
    pa, pb = _parts(a), _parts(b)
    while len(pa) < len(pb):
        pa.append(0)
    while len(pb) < len(pa):
        pb.append(0)
    return pa < pb


def calculate_risk_score(attestation: dict[str, Any]) -> int:
    """Return a 0..100 risk score (higher is worse)."""
    score = 0
    if not attestation.get("disk_encrypted"):
        score += 25
    if not attestation.get("screen_lock"):
        score += 15
    if attestation.get("jailbroken"):
        score += 50
    if not attestation.get("antivirus_active"):
        score += 10
    os_name = (attestation.get("os") or "").lower()
    os_version = (attestation.get("os_version") or "").lower()
    min_v = MIN_OS_VERSIONS.get(os_name)
    if min_v and os_version and _version_lt(os_version, min_v):
        score += 20
    if os_name == "" or os_version == "":
        score += 10
    return min(100, score)


class DevicePosture:
    async def submit(
        self,
        *,
        device_id: str,
        user_id: Optional[str],
        os: str,
        os_version: str,
        app_version: str,
        disk_encrypted: bool,
        screen_lock: bool,
        antivirus_active: bool,
        jailbroken: bool,
    ) -> DeviceAttestation:
        score = calculate_risk_score({
            "os": os, "os_version": os_version,
            "disk_encrypted": disk_encrypted,
            "screen_lock": screen_lock,
            "antivirus_active": antivirus_active,
            "jailbroken": jailbroken,
        })
        now = datetime.now(timezone.utc)
        row = DeviceAttestation(
            device_id=device_id,
            user_id=user_id,
            os=os,
            os_version=os_version,
            app_version=app_version,
            disk_encrypted=disk_encrypted,
            screen_lock=screen_lock,
            antivirus_active=antivirus_active,
            jailbroken=jailbroken,
            attested_at=now,
            valid_until=now + ATTESTATION_TTL,
            risk_score=score,
        )
        async with async_session_factory() as db:
            db.add(row)
            await db.commit()
            await db.refresh(row)
        return row

    async def latest_for_device(
        self, device_id: str,
    ) -> Optional[DeviceAttestation]:
        async with async_session_factory() as db:
            r = await db.execute(
                select(DeviceAttestation)
                .where(DeviceAttestation.device_id == device_id)
                .order_by(desc(DeviceAttestation.attested_at))
                .limit(1)
            )
            return r.scalar_one_or_none()

    async def is_attested(self, device_id: str) -> tuple[bool, int]:
        """Returns (attested_and_valid, risk_score)."""
        att = await self.latest_for_device(device_id)
        if att is None:
            return False, 100
        if att.valid_until < datetime.now(timezone.utc):
            return False, att.risk_score
        return True, att.risk_score


_posture: Optional[DevicePosture] = None


def get_device_posture() -> DevicePosture:
    global _posture
    if _posture is None:
        _posture = DevicePosture()
    return _posture
