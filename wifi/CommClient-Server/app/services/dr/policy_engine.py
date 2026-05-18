"""
DR v2 Policy Engine — cron-driven backup scheduler.

* In-memory scheduler with a 30 s tick (replaceable by APScheduler if the
  optional dep is installed).
* ``dry_run(policy)`` returns ``{files_count, estimated_size,
  scopes_covered}`` *without* actually copying anything.
* ``trigger_policy_now(policy_id, actor_id)`` enqueues a backup job via
  :mod:`backup_engine_v2`.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_log
from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.session import async_session_factory
from app.models.dr_v2 import DRPolicy
from app.services.dr.backup_engine_v2 import backup_engine_v2


logger = get_logger(__name__)


# ── tiny cron parser ────────────────────────────────────────────────


def _parse_cron(expr: str) -> Optional[Dict[str, str]]:
    """Return a token dict or None on failure.  Supports 5-field syntax."""
    parts = expr.strip().split()
    if len(parts) != 5:
        return None
    minute, hour, dom, month, dow = parts
    return {"minute": minute, "hour": hour, "dom": dom,
            "month": month, "dow": dow}


def _matches(token: str, value: int) -> bool:
    """Match a single cron token against an integer value."""
    token = token.strip()
    if token == "*":
        return True
    # step ranges: */N or 1-9/N
    if token.startswith("*/"):
        try:
            return value % int(token[2:]) == 0
        except ValueError:
            return False
    if "/" in token:
        base, step = token.split("/", 1)
        if "-" in base:
            lo, hi = base.split("-", 1)
            try:
                lo_i, hi_i, step_i = int(lo), int(hi), int(step)
                return value in range(lo_i, hi_i + 1, step_i)
            except ValueError:
                return False
    if "-" in token:
        lo, hi = token.split("-", 1)
        try:
            return int(lo) <= value <= int(hi)
        except ValueError:
            return False
    if "," in token:
        try:
            return value in {int(x) for x in token.split(",") if x.strip()}
        except ValueError:
            return False
    try:
        return int(token) == value
    except ValueError:
        return False


def _cron_matches_now(expr: str, now: datetime) -> bool:
    spec = _parse_cron(expr)
    if not spec:
        return False
    return (
        _matches(spec["minute"], now.minute)
        and _matches(spec["hour"], now.hour)
        and _matches(spec["dom"], now.day)
        and _matches(spec["month"], now.month)
        and _matches(spec["dow"], now.weekday())
    )


def _next_run_estimate(expr: str, ref: datetime) -> Optional[datetime]:
    """Cheap heuristic: scan up to 1440 minutes ahead for the next match."""
    for i in range(1, 1440 * 7):
        ts = ref + timedelta(minutes=i)
        if _cron_matches_now(expr, ts):
            return ts.replace(second=0, microsecond=0)
    return None


# ── policy CRUD ─────────────────────────────────────────────────────


async def list_policies() -> List[Dict[str, Any]]:
    async with async_session_factory() as db:
        rows = (await db.execute(
            select(DRPolicy).order_by(DRPolicy.created_at.desc())
        )).scalars().all()
    return [_serialize(r) for r in rows]


async def get_policy(policy_id: str) -> Optional[Dict[str, Any]]:
    async with async_session_factory() as db:
        r = (await db.execute(
            select(DRPolicy).where(DRPolicy.id == policy_id)
        )).scalar_one_or_none()
    return _serialize(r) if r else None


async def create_policy(body: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
    import uuid as _uuid
    pid = _uuid.uuid4().hex
    async with async_session_factory() as db:
        r = DRPolicy(
            id=pid,
            name=body["name"],
            description=body.get("description"),
            cron_schedule=body.get("cron_schedule", "0 2 * * *"),
            scope=body.get("scope", []),
            cadence=body.get("cadence", "full"),
            retention=body.get("retention", {}),
            encryption_key_ref=body.get("encryption_key_ref"),
            pre_hook=body.get("pre_hook"),
            post_hook=body.get("post_hook"),
            destinations=body.get("destinations", []),
            enabled=bool(body.get("enabled", True)),
        )
        nr = _next_run_estimate(r.cron_schedule, datetime.now(timezone.utc))
        if nr:
            r.next_run_at = nr
        db.add(r)
        await db.commit()
    audit_log("dr.v2.policy_created", user_id=actor_id,
              details={"policy_id": pid, "name": body["name"]})
    return await get_policy(pid)  # type: ignore[return-value]


async def update_policy(
    policy_id: str, body: Dict[str, Any], actor_id: str,
) -> Optional[Dict[str, Any]]:
    fields = {k: v for k, v in body.items() if k in {
        "name", "description", "cron_schedule", "scope", "cadence",
        "retention", "encryption_key_ref", "pre_hook", "post_hook",
        "destinations", "enabled",
    }}
    async with async_session_factory() as db:
        await db.execute(
            update(DRPolicy).where(DRPolicy.id == policy_id).values(**fields),
        )
        await db.commit()
    audit_log("dr.v2.policy_updated", user_id=actor_id,
              details={"policy_id": policy_id, "fields": list(fields.keys())})
    return await get_policy(policy_id)


async def delete_policy(policy_id: str, actor_id: str) -> bool:
    async with async_session_factory() as db:
        r = (await db.execute(
            select(DRPolicy).where(DRPolicy.id == policy_id)
        )).scalar_one_or_none()
        if r is None:
            return False
        await db.delete(r)
        await db.commit()
    audit_log("dr.v2.policy_deleted", user_id=actor_id,
              details={"policy_id": policy_id})
    return True


# ── dry-run ─────────────────────────────────────────────────────────


async def dry_run(policy_id: str) -> Dict[str, Any]:
    s = get_settings()
    pol = await get_policy(policy_id)
    if pol is None:
        raise LookupError(f"policy {policy_id} not found")

    scopes: List[str] = list(pol.get("scope") or [])
    if not scopes:
        # default scope: everything under PROJECT_ROOT/data/uploads + db file
        scopes = ["data/uploads", "data/app.db"]
    root = Path(getattr(s, "PROJECT_ROOT", "."))
    files_count = 0
    total_size = 0
    covered: List[str] = []
    for sc in scopes:
        p = root / sc
        if not p.exists():
            continue
        covered.append(str(p))
        if p.is_file():
            files_count += 1
            try:
                total_size += p.stat().st_size
            except OSError:
                pass
        else:
            for f in p.rglob("*"):
                if f.is_file():
                    files_count += 1
                    try:
                        total_size += f.stat().st_size
                    except OSError:
                        pass
    return {
        "policy_id": policy_id, "name": pol["name"],
        "scopes_covered": covered,
        "files_count": files_count,
        "estimated_size_bytes": total_size,
        "cadence": pol["cadence"],
        "next_run_at": pol["next_run_at"],
    }


# ── trigger / scheduler ──────────────────────────────────────────────


async def trigger_policy_now(policy_id: str, actor_id: str) -> str:
    pol = await get_policy(policy_id)
    if pol is None:
        raise LookupError(f"policy {policy_id} not found")
    destination_id = None
    if pol["destinations"]:
        first = pol["destinations"][0]
        destination_id = first.get("id") if isinstance(first, dict) else first
    return await backup_engine_v2.start_backup(
        policy_id=policy_id,
        destination_id=destination_id,
        cadence=pol["cadence"],
        actor_id=actor_id,
    )


class PolicyScheduler:
    def __init__(self, tick_sec: float = 30.0) -> None:
        self._tick = tick_sec
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._last_fired: Dict[str, datetime] = {}

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="dr_v2_policy_scheduler")
        logger.info("dr_v2_policy_scheduler_started")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()
        self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._fire_due()
            except Exception:
                logger.exception("dr_v2_policy_scheduler_iteration_failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._tick)
            except asyncio.TimeoutError:
                pass

    async def _fire_due(self) -> None:
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        async with async_session_factory() as db:
            rows = (await db.execute(
                select(DRPolicy).where(DRPolicy.enabled.is_(True))
            )).scalars().all()
        for p in rows:
            if not p.cron_schedule:
                continue
            if not _cron_matches_now(p.cron_schedule, now):
                continue
            last = self._last_fired.get(p.id)
            if last and (now - last).total_seconds() < 50:
                continue
            self._last_fired[p.id] = now
            try:
                job_id = await trigger_policy_now(p.id, actor_id="scheduler")
                async with async_session_factory() as db:
                    await db.execute(
                        update(DRPolicy).where(DRPolicy.id == p.id).values(
                            last_run_at=now,
                            next_run_at=_next_run_estimate(
                                p.cron_schedule, now,
                            ),
                        )
                    )
                    await db.commit()
                logger.info("dr_v2_policy_fired", policy_id=p.id, job_id=job_id)
            except Exception:
                logger.exception("dr_v2_policy_fire_failed", policy_id=p.id)


policy_scheduler = PolicyScheduler()


# ── serialization ───────────────────────────────────────────────────


def _serialize(r: Optional[DRPolicy]) -> Optional[Dict[str, Any]]:
    if r is None:
        return None
    return {
        "id": r.id, "name": r.name, "description": r.description,
        "cron_schedule": r.cron_schedule,
        "scope": list(r.scope or []),
        "cadence": r.cadence,
        "retention": dict(r.retention or {}),
        "encryption_key_ref": r.encryption_key_ref,
        "pre_hook": r.pre_hook, "post_hook": r.post_hook,
        "destinations": list(r.destinations or []),
        "enabled": r.enabled,
        "last_run_at": r.last_run_at.isoformat() if r.last_run_at else None,
        "next_run_at": r.next_run_at.isoformat() if r.next_run_at else None,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }
