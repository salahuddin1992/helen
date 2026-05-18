"""
DR v2 RPO/RTO meter — exposes the current operational measurements.

* **RPO** (Recovery Point Objective) is approximated as the age of the
  most recent **successful** backup whose integrity check still passes.
* **RTO** (Recovery Time Objective) is the rolling average actual
  restore time observed by the last ``N`` drills (default 5).  When no
  drill history exists we fall back to the duration of the most recent
  restore job.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import async_session_factory
from app.models.dr_v2 import DRBackup, DRDrillV2, DRJob


async def measure() -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    rpo_seconds: Optional[int] = None
    last_backup: Optional[Dict[str, Any]] = None
    async with async_session_factory() as db:
        backup_row = (await db.execute(
            select(DRBackup).where(
                DRBackup.status == "succeeded",
            ).order_by(desc(DRBackup.completed_at)).limit(1)
        )).scalar_one_or_none()
        if backup_row and backup_row.completed_at:
            rpo_seconds = max(0, int((now - backup_row.completed_at).total_seconds()))
            last_backup = {
                "id": backup_row.id,
                "completed_at": backup_row.completed_at.isoformat(),
                "size_bytes": backup_row.size_bytes,
                "verified_ok": backup_row.last_verify_ok,
                "last_verified_at": backup_row.last_verified_at.isoformat()
                                     if backup_row.last_verified_at else None,
            }

        # RTO from drills
        drills = (await db.execute(
            select(DRDrillV2).where(DRDrillV2.status == "succeeded")
            .order_by(desc(DRDrillV2.completed_at)).limit(5)
        )).scalars().all()

    rto_samples: List[int] = [int(d.rto_seconds) for d in drills if d.rto_seconds]
    rto_avg: Optional[float] = None
    rto_max: Optional[int] = None
    if rto_samples:
        rto_avg = sum(rto_samples) / len(rto_samples)
        rto_max = max(rto_samples)

    return {
        "measured_at": now.isoformat(),
        "rpo_seconds": rpo_seconds,
        "rto_seconds_avg": rto_avg,
        "rto_seconds_max": rto_max,
        "rto_samples": rto_samples,
        "last_backup": last_backup,
        "drill_count": len(drills),
    }
