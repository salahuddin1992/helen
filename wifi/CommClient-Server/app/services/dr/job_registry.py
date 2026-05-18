"""
In-process job registry for DR v2.

Tracks ``DRJob`` rows + an in-memory progress channel so the WebSocket
manager can push real-time updates without polling the DB.  Persistent
state lives in ``dr_v2_jobs``; the registry is just a tap on top of it.
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.session import async_session_factory
from app.models.dr_v2 import DRJob


logger = get_logger(__name__)


@dataclass
class JobSnapshot:
    id: str
    kind: str
    status: str
    progress: int
    message: Optional[str]
    backup_id: Optional[str]
    policy_id: Optional[str]
    destination_id: Optional[str]
    actor_id: Optional[str]
    created_at: str
    started_at: Optional[str]
    completed_at: Optional[str]
    payload: Dict[str, Any] = field(default_factory=dict)
    result: Dict[str, Any] = field(default_factory=dict)
    error_message: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "kind": self.kind, "status": self.status,
            "progress": self.progress, "message": self.message,
            "backup_id": self.backup_id, "policy_id": self.policy_id,
            "destination_id": self.destination_id,
            "actor_id": self.actor_id,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "payload": dict(self.payload), "result": dict(self.result),
            "error_message": self.error_message,
        }


class JobRegistry:
    def __init__(self) -> None:
        self._listeners: Set[asyncio.Queue] = set()
        self._cancel_flags: Dict[str, bool] = {}
        self._lock = asyncio.Lock()

    # ── pubsub ────────────────────────────────────────────────────

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=512)
        self._listeners.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._listeners.discard(q)

    async def _emit(self, event: str, data: Dict[str, Any]) -> None:
        payload = {"event": event, "data": data,
                   "ts": datetime.now(timezone.utc).isoformat()}
        for q in list(self._listeners):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                logger.warning("dr_v2_job_listener_dropped")

    # ── job lifecycle ─────────────────────────────────────────────

    async def create(
        self,
        kind: str,
        *,
        backup_id: Optional[str] = None,
        policy_id: Optional[str] = None,
        destination_id: Optional[str] = None,
        actor_id: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> str:
        job_id = uuid.uuid4().hex
        async with async_session_factory() as db:
            db.add(DRJob(
                id=job_id, kind=kind, status="queued",
                backup_id=backup_id, policy_id=policy_id,
                destination_id=destination_id, actor_id=actor_id,
                payload=payload or {},
                created_at=datetime.now(timezone.utc),
            ))
            await db.commit()
        await self._emit("job.update", {"id": job_id, "kind": kind,
                                         "status": "queued"})
        return job_id

    async def start(self, job_id: str, message: str = "started") -> None:
        async with async_session_factory() as db:
            await db.execute(
                update(DRJob).where(DRJob.id == job_id).values(
                    status="running",
                    started_at=datetime.now(timezone.utc),
                    progress_message=message,
                )
            )
            await db.commit()
        await self._emit("job.update", {"id": job_id, "status": "running",
                                         "message": message})

    async def progress(
        self, job_id: str, pct: int, message: Optional[str] = None,
    ) -> None:
        async with async_session_factory() as db:
            await db.execute(
                update(DRJob).where(DRJob.id == job_id).values(
                    progress=max(0, min(100, int(pct))),
                    progress_message=message,
                )
            )
            await db.commit()
        await self._emit("job.progress", {"id": job_id, "progress": pct,
                                           "message": message})

    async def finish(
        self,
        job_id: str,
        *,
        status: str = "succeeded",
        result: Optional[Dict[str, Any]] = None,
        error_message: Optional[str] = None,
    ) -> None:
        async with async_session_factory() as db:
            await db.execute(
                update(DRJob).where(DRJob.id == job_id).values(
                    status=status,
                    progress=100 if status == "succeeded" else 100,
                    completed_at=datetime.now(timezone.utc),
                    result=result or {},
                    error_message=error_message,
                )
            )
            await db.commit()
        await self._emit("job.update", {"id": job_id, "status": status,
                                         "result": result or {},
                                         "error_message": error_message})

    async def cancel(self, job_id: str) -> bool:
        async with self._lock:
            self._cancel_flags[job_id] = True
        async with async_session_factory() as db:
            row = (await db.execute(
                select(DRJob).where(DRJob.id == job_id)
            )).scalar_one_or_none()
            if row is None:
                return False
            if row.status in ("succeeded", "failed", "cancelled"):
                return False
            await db.execute(
                update(DRJob).where(DRJob.id == job_id).values(
                    status="cancelled",
                    completed_at=datetime.now(timezone.utc),
                    progress_message="cancelled by operator",
                )
            )
            await db.commit()
        await self._emit("job.update", {"id": job_id, "status": "cancelled"})
        return True

    def is_cancelled(self, job_id: str) -> bool:
        return bool(self._cancel_flags.get(job_id, False))

    # ── readers ───────────────────────────────────────────────────

    async def get(self, job_id: str) -> Optional[JobSnapshot]:
        async with async_session_factory() as db:
            r = (await db.execute(
                select(DRJob).where(DRJob.id == job_id)
            )).scalar_one_or_none()
        if r is None:
            return None
        return self._snapshot(r)

    async def list(
        self,
        *,
        kind: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> List[JobSnapshot]:
        async with async_session_factory() as db:
            q = select(DRJob).order_by(DRJob.created_at.desc()).limit(limit)
            if kind:
                q = q.where(DRJob.kind == kind)
            if status:
                q = q.where(DRJob.status == status)
            rows = (await db.execute(q)).scalars().all()
        return [self._snapshot(r) for r in rows]

    @staticmethod
    def _snapshot(r: DRJob) -> JobSnapshot:
        return JobSnapshot(
            id=r.id, kind=r.kind, status=r.status,
            progress=r.progress or 0,
            message=r.progress_message,
            backup_id=r.backup_id, policy_id=r.policy_id,
            destination_id=r.destination_id,
            actor_id=r.actor_id,
            created_at=r.created_at.isoformat() if r.created_at else "",
            started_at=r.started_at.isoformat() if r.started_at else None,
            completed_at=r.completed_at.isoformat() if r.completed_at else None,
            payload=dict(r.payload or {}),
            result=dict(r.result or {}),
            error_message=r.error_message,
        )


dr_job_registry = JobRegistry()
