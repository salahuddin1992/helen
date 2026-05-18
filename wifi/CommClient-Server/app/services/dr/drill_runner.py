"""
DR v2 DrillRunner — restores into an isolated sandbox and produces a
full report (steps, RTO measured, integrity check, recommendations).
"""
from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import desc, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_log
from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.session import async_session_factory
from app.models.dr_v2 import DRBackup, DRDrillV2
from app.services.dr.backup_engine_v2 import backup_engine_v2
from app.services.dr.integrity_verifier import integrity_verifier
from app.services.dr.job_registry import dr_job_registry


logger = get_logger(__name__)


class DrillRunner:
    async def schedule(
        self,
        *,
        scheduled_at: datetime,
        scope: str = "sandbox",
        name: Optional[str] = None,
        actor_id: Optional[str] = None,
    ) -> str:
        did = uuid.uuid4().hex
        async with async_session_factory() as db:
            db.add(DRDrillV2(
                id=did, name=name, status="scheduled",
                scheduled_at=scheduled_at, scope=scope,
                actor_id=actor_id,
            ))
            await db.commit()
        audit_log("dr.v2.drill_scheduled", user_id=actor_id or "system",
                  details={"drill_id": did, "scope": scope,
                           "scheduled_at": scheduled_at.isoformat()})
        return did

    async def run_drill(
        self,
        *,
        scope: str = "sandbox",
        actor_id: Optional[str] = None,
        backup_id: Optional[str] = None,
        name: Optional[str] = None,
        drill_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        did = drill_id or uuid.uuid4().hex
        t0 = time.perf_counter()
        started_at = datetime.now(timezone.utc)

        async with async_session_factory() as db:
            if drill_id is None:
                db.add(DRDrillV2(
                    id=did, name=name, status="running",
                    scheduled_at=started_at, started_at=started_at,
                    scope=scope, actor_id=actor_id,
                ))
            else:
                await db.execute(
                    update(DRDrillV2).where(DRDrillV2.id == did).values(
                        status="running", started_at=started_at,
                    )
                )
            await db.commit()

        steps: List[Dict[str, Any]] = []
        recommendations: List[str] = []
        integrity_ok = False
        rpo_seconds = 0
        try:
            # 1. choose target backup
            steps.append({"step": "select-backup", "at": datetime.now(timezone.utc).isoformat()})
            if backup_id is None:
                async with async_session_factory() as db:
                    row = (await db.execute(
                        select(DRBackup).where(DRBackup.status == "succeeded")
                        .order_by(desc(DRBackup.started_at)).limit(1)
                    )).scalar_one_or_none()
                if row is None:
                    raise RuntimeError("no successful backup available")
                backup_id = row.id
                if row.completed_at:
                    rpo_seconds = max(0, int((started_at - row.completed_at).total_seconds()))

            # 2. integrity verify
            steps.append({"step": "verify-integrity",
                          "backup_id": backup_id,
                          "at": datetime.now(timezone.utc).isoformat()})
            vres = await integrity_verifier.verify_backup(backup_id)
            integrity_ok = bool(vres.get("ok"))
            if not integrity_ok:
                recommendations.append(
                    "Re-run backup — integrity check failed during drill.",
                )

            # 3. restore to sandbox
            steps.append({"step": "restore-sandbox", "at": datetime.now(timezone.utc).isoformat()})
            job_id = await backup_engine_v2.restore(
                backup_id, target="sandbox", scope=scope,
                reason=f"DR drill {did}",
                actor_id=actor_id or "drill-runner",
                confirmation="RESTORE",
            )
            # poll
            while True:
                snap = await dr_job_registry.get(job_id)
                if snap is None:
                    break
                if snap.status in ("succeeded", "failed", "cancelled"):
                    steps.append({"step": "restore-finished",
                                  "status": snap.status,
                                  "at": datetime.now(timezone.utc).isoformat()})
                    break
                await asyncio.sleep(1.0)

            rto_seconds = int(time.perf_counter() - t0)
            success = integrity_ok and snap is not None and snap.status == "succeeded"

            if rto_seconds > 1800:
                recommendations.append(
                    f"RTO of {rto_seconds}s exceeds 30-minute SLA — consider "
                    f"faster destination or incremental chunking.",
                )
            if rpo_seconds > 24 * 3600:
                recommendations.append(
                    "Most recent backup is older than 24h — tighten policy cadence.",
                )
            if not recommendations:
                recommendations.append("All checks passed — no changes recommended.")

            report = {
                "backup_id": backup_id, "scope": scope,
                "rto_seconds": rto_seconds, "rpo_seconds": rpo_seconds,
                "integrity_ok": integrity_ok,
                "steps": steps, "recommendations": recommendations,
                "verifier": vres,
                "restore_job_id": job_id,
            }

            async with async_session_factory() as db:
                await db.execute(
                    update(DRDrillV2).where(DRDrillV2.id == did).values(
                        status="succeeded" if success else "failed",
                        completed_at=datetime.now(timezone.utc),
                        rto_seconds=rto_seconds, rpo_seconds=rpo_seconds,
                        integrity_ok=integrity_ok,
                        steps=steps, recommendations=recommendations,
                        report=report,
                    )
                )
                await db.commit()
            audit_log("dr.v2.drill_completed", user_id=actor_id or "system",
                      success=success,
                      details={"drill_id": did, "rto": rto_seconds,
                               "rpo": rpo_seconds, "integrity_ok": integrity_ok})
            return {"drill_id": did, **report, "success": success}
        except Exception as e:
            logger.exception("dr_v2_drill_failed", drill_id=did)
            async with async_session_factory() as db:
                await db.execute(
                    update(DRDrillV2).where(DRDrillV2.id == did).values(
                        status="failed",
                        completed_at=datetime.now(timezone.utc),
                        steps=steps,
                        recommendations=recommendations + [f"error: {e}"],
                        report={"error": str(e), "steps": steps},
                    )
                )
                await db.commit()
            audit_log("dr.v2.drill_failed", user_id=actor_id or "system",
                      success=False,
                      details={"drill_id": did, "error": str(e)})
            raise


drill_runner = DrillRunner()
