"""
Phase 6 / Module AA — periodic DR drills.

Every ``DR_DRILL_INTERVAL_HOURS`` (default 720h ≈ 30 days) the scheduler:

  1.  Locates the most recent successful ``RestorePoint``.
  2.  Runs ``restore_engine.simulate_restore`` against a sandboxed dir.
  3.  Measures wall-clock RTO and the staleness of the recovered data (RPO).
  4.  Persists a ``DRDrill`` row and, on failure, fires a webhook+email
      notification.

The loop is intentionally robust: any exception inside one iteration is
logged and swallowed so the timer keeps running.
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import desc, select

from app.core.logging import get_logger
from app.db.session import async_session_factory
from app.models.dr import DRDrill, RestorePoint
from app.services.dr.restore_engine import restore_engine

logger = get_logger(__name__)


@dataclass
class DrillState:
    enabled: bool = False
    interval_hours: float = 720.0
    last_success: Optional[datetime] = None
    last_attempt: Optional[datetime] = None
    last_rto_seconds: int = 0
    last_rpo_seconds: int = 0
    run_count: int = 0
    failure_count: int = 0


_state = DrillState()
_task: Optional[asyncio.Task] = None


def get_state() -> DrillState:
    return _state


async def _notify_failure(report: dict) -> None:
    """Best-effort notification — webhook v2 + email if configured."""
    try:
        from app.services.webhooks_v2.event_bus import publish              # noqa: F401
        await publish("dr.drill_failed", report)
    except Exception:                                                       # pragma: no cover
        pass
    try:
        # naive email — if helen has an email service it will be picked up
        from app.services.email_service import email_service                # type: ignore
        admins = os.environ.get("HELEN_DR_ALERT_EMAILS", "").split(",")
        for addr in admins:
            addr = addr.strip()
            if addr:
                await email_service.send(
                    to=addr, subject="[Helen] DR drill failed",
                    body=str(report)[:4000],
                )
    except Exception:                                                       # pragma: no cover
        pass


async def _pick_restore_point() -> Optional[RestorePoint]:
    async with async_session_factory() as db:
        return (await db.execute(
            select(RestorePoint).order_by(desc(RestorePoint.created_at)).limit(1)
        )).scalar_one_or_none()


async def run_once() -> dict:
    """Single drill iteration. Returns a structured report."""
    t0 = datetime.now(timezone.utc)
    _state.last_attempt = t0
    rp = await _pick_restore_point()
    if rp is None:
        report = {"ok": False, "reason": "no restore point available"}
        async with async_session_factory() as db:
            db.add(DRDrill(
                scheduled_at=t0, executed_at=t0, success=False,
                rto_seconds=0, rpo_seconds=0, report=report,
            ))
            await db.commit()
        _state.failure_count += 1
        return report

    rpo = int((t0 - rp.created_at).total_seconds()) if rp.created_at else 0
    sim = await restore_engine.simulate_restore(rp.id)
    t1 = datetime.now(timezone.utc)
    rto = int((t1 - t0).total_seconds())
    ok = bool(sim.get("ok"))
    report = {
        "ok": ok, "restore_point_id": rp.id,
        "rto_seconds": rto, "rpo_seconds": rpo,
        "details": sim,
    }
    async with async_session_factory() as db:
        db.add(DRDrill(
            scheduled_at=t0, executed_at=t1, success=ok,
            rto_seconds=rto, rpo_seconds=rpo, report=report,
        ))
        await db.commit()

    _state.run_count += 1
    _state.last_rto_seconds = rto
    _state.last_rpo_seconds = rpo
    if ok:
        _state.last_success = t1
    else:
        _state.failure_count += 1
        await _notify_failure(report)
    return report


async def _loop() -> None:
    await asyncio.sleep(60)  # let the server warm up
    while True:
        try:
            await run_once()
        except asyncio.CancelledError:
            raise
        except Exception as e:                                              # pragma: no cover
            logger.exception("dr_drill_loop_error", error=str(e))
        await asyncio.sleep(max(60.0, _state.interval_hours * 3600.0))


async def start(interval_hours: float = 720.0) -> None:
    global _task
    _state.enabled = True
    _state.interval_hours = float(interval_hours)
    if _task and not _task.done():
        return
    _task = asyncio.create_task(_loop(), name="dr_drill_scheduler")
    logger.info("dr_drill_scheduler_started", interval_hours=interval_hours)


async def stop() -> None:
    global _task
    _state.enabled = False
    if _task and not _task.done():
        _task.cancel()
        try:
            await _task
        except (asyncio.CancelledError, Exception):
            pass
    _task = None


async def rto_rpo_summary() -> dict:
    """Recent drill metrics for the admin dashboard."""
    async with async_session_factory() as db:
        rows = (await db.execute(
            select(DRDrill).order_by(desc(DRDrill.executed_at)).limit(20)
        )).scalars().all()
    if not rows:
        return {"count": 0, "avg_rto": 0, "avg_rpo": 0, "success_rate": None}
    rtos = [r.rto_seconds for r in rows if r.executed_at]
    rpos = [r.rpo_seconds for r in rows if r.executed_at]
    ok = [r for r in rows if r.success]
    return {
        "count": len(rows),
        "avg_rto": sum(rtos) // max(1, len(rtos)),
        "avg_rpo": sum(rpos) // max(1, len(rpos)),
        "max_rto": max(rtos) if rtos else 0,
        "max_rpo": max(rpos) if rpos else 0,
        "success_rate": len(ok) / len(rows),
    }
