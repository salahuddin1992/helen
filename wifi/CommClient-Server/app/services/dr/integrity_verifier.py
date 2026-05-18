"""
DR v2 IntegrityVerifier — chunk-level Merkle re-verification.

A queue + worker that pulls a backup, re-fetches every chunk from its
destination, recomputes the SHA-256, compares against the persisted
manifest, and updates ``last_verified_at`` / ``last_verify_ok``.

Mismatches fire an audit alert (``dr.v2.integrity_alert``) and surface
in the admin UI ``/verify/alerts`` queue.
"""
from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import desc, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_log
from app.core.logging import get_logger
from app.db.session import async_session_factory
from app.models.dr_v2 import (
    DRBackup,
    DRBackupChunk,
    DRDestination,
)
from app.services.dr.destination_drivers import build_driver
from app.services.dr.job_registry import dr_job_registry


logger = get_logger(__name__)


@dataclass
class VerifyAlert:
    backup_id: str
    raised_at: str
    reason: str
    details: Dict[str, Any] = field(default_factory=dict)


class IntegrityVerifier:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=4096)
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._alerts: List[VerifyAlert] = []
        self._alerts_max = 1000

    # ── service lifecycle ────────────────────────────────────────

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._worker_loop(), name="dr_v2_integrity_verifier")
        logger.info("dr_v2_integrity_verifier_started")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()
        self._task = None

    # ── public ────────────────────────────────────────────────────

    async def queue_verify(self, backup_id: str) -> None:
        await self._queue.put(backup_id)

    async def verify_backup(self, backup_id: str) -> Dict[str, Any]:
        return await self._verify(backup_id)

    async def run_full_corpus(self) -> Dict[str, Any]:
        async with async_session_factory() as db:
            ids = (await db.execute(
                select(DRBackup.id).order_by(desc(DRBackup.started_at))
            )).scalars().all()
        for i in ids:
            await self._queue.put(i)
        return {"queued": len(ids)}

    def queue_size(self) -> int:
        return self._queue.qsize()

    def alerts(self) -> List[Dict[str, Any]]:
        return [
            {"backup_id": a.backup_id, "raised_at": a.raised_at,
             "reason": a.reason, "details": dict(a.details)}
            for a in self._alerts[-self._alerts_max:]
        ]

    # ── internals ─────────────────────────────────────────────────

    async def _worker_loop(self) -> None:
        while not self._stop.is_set():
            try:
                backup_id = await asyncio.wait_for(self._queue.get(), timeout=2.0)
            except asyncio.TimeoutError:
                continue
            try:
                await self._verify(backup_id)
            except Exception:
                logger.exception("dr_v2_verify_iteration_failed",
                                 backup_id=backup_id)
            finally:
                self._queue.task_done()

    async def _verify(self, backup_id: str) -> Dict[str, Any]:
        t0 = time.perf_counter()
        job_id = await dr_job_registry.create(
            kind="verify", backup_id=backup_id, actor_id="verifier",
        )
        await dr_job_registry.start(job_id, "loading manifest")
        try:
            async with async_session_factory() as db:
                backup = (await db.execute(
                    select(DRBackup).where(DRBackup.id == backup_id)
                )).scalar_one_or_none()
                if backup is None:
                    raise LookupError(f"backup {backup_id} not found")
                chunks = (await db.execute(
                    select(DRBackupChunk).where(DRBackupChunk.backup_id == backup_id)
                    .order_by(DRBackupChunk.seq.asc())
                )).scalars().all()
                dest_row: Optional[DRDestination] = None
                if backup.destination_id:
                    dest_row = (await db.execute(
                        select(DRDestination).where(DRDestination.id == backup.destination_id)
                    )).scalar_one_or_none()

            if dest_row is None:
                raise RuntimeError("destination missing")
            driver = build_driver(dest_row.kind, dest_row.config or {})

            mismatched: List[int] = []
            await dr_job_registry.progress(job_id, 10, "downloading chunks")
            total = max(1, len(chunks))
            chunk_hashes: List[str] = []
            for idx, c in enumerate(chunks):
                body = await driver.read_chunk(f"backups/{backup_id}", c.seq)
                h = hashlib.sha256(body).hexdigest()
                chunk_hashes.append(h)
                if h != c.sha256:
                    mismatched.append(c.seq)
                if idx % 4 == 0:
                    await dr_job_registry.progress(
                        job_id,
                        min(10 + int(80 * (idx + 1) / total), 90),
                        f"chunk {idx+1}/{total}",
                    )

            root = hashlib.sha256("".join(chunk_hashes).encode()).hexdigest()
            ok = (not mismatched) and root == (backup.sha256_root or "")

            now = datetime.now(timezone.utc)
            async with async_session_factory() as db:
                await db.execute(
                    update(DRBackup).where(DRBackup.id == backup_id).values(
                        last_verified_at=now, last_verify_ok=ok,
                    )
                )
                await db.commit()

            if not ok:
                alert = VerifyAlert(
                    backup_id=backup_id,
                    raised_at=now.isoformat(),
                    reason="merkle mismatch" if mismatched or root != backup.sha256_root else "unknown",
                    details={"mismatched_chunks": mismatched,
                             "computed_root": root,
                             "expected_root": backup.sha256_root},
                )
                self._alerts.append(alert)
                if len(self._alerts) > self._alerts_max:
                    self._alerts.pop(0)
                audit_log("dr.v2.integrity_alert", user_id="system",
                          success=False,
                          details={"backup_id": backup_id,
                                   "mismatched": mismatched,
                                   "expected_root": backup.sha256_root,
                                   "computed_root": root})

            result = {
                "ok": ok, "backup_id": backup_id,
                "mismatched_chunks": mismatched,
                "duration_sec": time.perf_counter() - t0,
                "computed_root": root,
                "expected_root": backup.sha256_root,
            }
            await dr_job_registry.finish(
                job_id, status="succeeded" if ok else "failed",
                result=result,
            )
            return result
        except Exception as e:
            await dr_job_registry.finish(
                job_id, status="failed", error_message=str(e)[:2000],
            )
            raise


integrity_verifier = IntegrityVerifier()
