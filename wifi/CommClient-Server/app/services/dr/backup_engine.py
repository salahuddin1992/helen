"""
Phase 6 / Module AA — central backup engine.

Responsibilities
----------------
* Take a consistent SQLite snapshot using ``sqlite3.Connection.backup()`` —
  no downtime, no half-flushed WAL pages.
* Snapshot the file-store directories (UPLOAD_DIR + data dir) into a tar
  stream, optionally encrypted + compressed via :mod:`encryption`.
* Persist progress and metadata into the ``dr_backup_jobs`` table.
* Drive an arbitrary :class:`BackupDestination` adapter for upload.
* Enforce retention: keep N fulls + M incrementals, prune older.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import desc, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.session import async_session_factory
from app.models.dr import (
    VALID_BACKUP_KINDS,
    BackupDestination as DBBackupDestination,
    BackupJob,
    RestorePoint,
)
from app.services.dr import encryption as dr_crypto
from app.services.dr.destinations import (
    BackupDestination,
    LocalDestination,
    build_destination,
)

logger = get_logger(__name__)


_HEAD = 1024 * 1024


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            b = f.read(_HEAD)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


@dataclass
class BackupResult:
    job_id: str
    path: Path
    size_bytes: int
    sha256: str
    encrypted: bool
    key_id: Optional[str]
    duration_sec: float
    destination: Optional[str]
    manifest: Dict[str, Any]


class BackupEngine:
    """The single class admin endpoints + drill scheduler invoke."""

    def __init__(self) -> None:
        s = get_settings()
        self._project_root = Path(getattr(s, "PROJECT_ROOT", "."))
        self._db_path = Path(getattr(s, "SQLITE_PATH", "data/app.db"))
        if not self._db_path.is_absolute():
            self._db_path = (self._project_root / self._db_path).resolve()
        self._upload_dir = Path(getattr(s, "UPLOAD_DIR", "data/uploads"))
        if not self._upload_dir.is_absolute():
            self._upload_dir = (self._project_root / self._upload_dir).resolve()
        self._data_dir = (self._project_root / "data").resolve()
        self._stage_root = self._data_dir / "dr" / "stage"
        self._stage_root.mkdir(parents=True, exist_ok=True)
        self._app_version = str(getattr(s, "APP_VERSION", "dev"))
        self._schema_version = str(getattr(s, "SCHEMA_VERSION", "v1"))
        self._lock = asyncio.Lock()

    # ── public API ──────────────────────────────────────────────

    async def create_full(
        self,
        *,
        destination: Optional[BackupDestination] = None,
        destination_id: Optional[str] = None,
        encrypt: bool = True,
        retention_days: Optional[int] = 30,
    ) -> BackupResult:
        return await self._create(
            kind="full", base_job_id=None,
            destination=destination, destination_id=destination_id,
            encrypt=encrypt, retention_days=retention_days,
        )

    async def create_incremental(
        self,
        base_job_id: str,
        *,
        destination: Optional[BackupDestination] = None,
        destination_id: Optional[str] = None,
        encrypt: bool = True,
        retention_days: Optional[int] = 14,
    ) -> BackupResult:
        return await self._create(
            kind="incremental", base_job_id=base_job_id,
            destination=destination, destination_id=destination_id,
            encrypt=encrypt, retention_days=retention_days,
        )

    async def create_snapshot(
        self,
        *,
        destination: Optional[BackupDestination] = None,
        destination_id: Optional[str] = None,
        encrypt: bool = False,
        retention_days: Optional[int] = 7,
    ) -> BackupResult:
        """Filesystem snapshot — fastest, no compression by default."""
        return await self._create(
            kind="snapshot", base_job_id=None,
            destination=destination, destination_id=destination_id,
            encrypt=encrypt, retention_days=retention_days,
        )

    async def rotate(
        self,
        *,
        keep_full: int = 7,
        keep_incremental: int = 14,
        destination_id: Optional[str] = None,
    ) -> Dict[str, int]:
        """Prune older jobs of each kind.  Returns a count summary."""
        pruned = {"full": 0, "incremental": 0, "snapshot": 0}
        async with async_session_factory() as db:
            for kind, keep in (
                ("full", keep_full),
                ("incremental", keep_incremental),
                ("snapshot", max(3, keep_incremental)),
            ):
                q = select(BackupJob).where(
                    BackupJob.kind == kind,
                    BackupJob.status == "succeeded",
                )
                if destination_id:
                    q = q.where(BackupJob.destination_id == destination_id)
                q = q.order_by(desc(BackupJob.completed_at)).offset(keep)
                jobs = (await db.execute(q)).scalars().all()
                for j in jobs:
                    if j.destination and Path(j.destination).exists():
                        try:
                            Path(j.destination).unlink()
                        except OSError as e:
                            logger.warning(
                                "dr_rotate_unlink_failed",
                                job_id=j.id, path=j.destination, error=str(e),
                            )
                    await db.delete(j)
                    pruned[kind] += 1
            await db.commit()
        return pruned

    # ── internal pipeline ───────────────────────────────────────

    async def _create(
        self,
        *,
        kind: str,
        base_job_id: Optional[str],
        destination: Optional[BackupDestination],
        destination_id: Optional[str],
        encrypt: bool,
        retention_days: Optional[int],
    ) -> BackupResult:
        if kind not in VALID_BACKUP_KINDS:
            raise ValueError(f"invalid backup kind: {kind!r}")

        async with self._lock:
            t0 = datetime.now(timezone.utc)
            job = BackupJob(
                id=uuid.uuid4().hex,
                kind=kind,
                status="running",
                started_at=t0,
                base_job_id=base_job_id,
                destination_id=destination_id,
                encrypted=bool(encrypt),
                manifest={},
            )
            async with async_session_factory() as db:
                db.add(job)
                await db.commit()

            try:
                result = await asyncio.to_thread(
                    self._pipeline, kind, base_job_id, encrypt,
                )
                final_dest: Optional[str] = None
                if destination or destination_id:
                    dest = destination or await self._resolve_destination(destination_id)
                    info = await dest.upload(result.path, result.path.name)
                    final_dest = info.get("path") or info.get("key") or result.path.name
                else:
                    final_dest = str(result.path)

                t1 = datetime.now(timezone.utc)
                retention_until = (
                    t1 + timedelta(days=int(retention_days))
                    if retention_days else None
                )

                async with async_session_factory() as db:
                    await db.execute(
                        update(BackupJob).where(BackupJob.id == job.id).values(
                            status="succeeded",
                            completed_at=t1,
                            size_bytes=result.size_bytes,
                            sha256=result.sha256,
                            destination=final_dest,
                            retention_until=retention_until,
                            encrypted_key_ref=result.key_id,
                            manifest=result.manifest,
                        )
                    )
                    rp = RestorePoint(
                        id=uuid.uuid4().hex,
                        backup_job_id=job.id,
                        schema_version=self._schema_version,
                        app_version=self._app_version,
                        manifest=result.manifest,
                        created_at=t1,
                    )
                    db.add(rp)
                    await db.commit()

                logger.info(
                    "dr_backup_succeeded", job_id=job.id, kind=kind,
                    size=result.size_bytes, encrypted=result.encrypted,
                )
                return BackupResult(
                    job_id=job.id,
                    path=result.path,
                    size_bytes=result.size_bytes,
                    sha256=result.sha256,
                    encrypted=result.encrypted,
                    key_id=result.key_id,
                    duration_sec=(t1 - t0).total_seconds(),
                    destination=final_dest,
                    manifest=result.manifest,
                )

            except Exception as e:
                logger.exception("dr_backup_failed", job_id=job.id, error=str(e))
                async with async_session_factory() as db:
                    await db.execute(
                        update(BackupJob).where(BackupJob.id == job.id).values(
                            status="failed",
                            completed_at=datetime.now(timezone.utc),
                            error_message=str(e)[:2000],
                        )
                    )
                    await db.commit()
                raise

    async def _resolve_destination(self, destination_id: str) -> BackupDestination:
        async with async_session_factory() as db:
            row = (await db.execute(
                select(DBBackupDestination).where(
                    DBBackupDestination.id == destination_id,
                )
            )).scalar_one_or_none()
        if row is None:
            raise LookupError(f"destination {destination_id} not found")
        return build_destination(row.kind, row.config or {})

    # ── synchronous helpers (executed in worker thread) ─────────

    def _pipeline(
        self,
        kind: str,
        base_job_id: Optional[str],
        encrypt: bool,
    ) -> BackupResult:
        with tempfile.TemporaryDirectory(prefix="dr_stage_", dir=str(self._stage_root)) as td:
            stage = Path(td)
            db_copy = stage / "app.db"
            manifest: Dict[str, Any] = {
                "kind": kind,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "app_version": self._app_version,
                "schema_version": self._schema_version,
                "files": [],
            }

            # 1. SQLite online backup
            if self._db_path.exists():
                self._sqlite_online_backup(self._db_path, db_copy)
                manifest["files"].append({
                    "kind": "sqlite",
                    "name": "app.db",
                    "sha256": _sha256_file(db_copy),
                    "size": db_copy.stat().st_size,
                })

            # 2. Filestore archive (skip on incremental — diff against base)
            since: Optional[datetime] = None
            if kind == "incremental" and base_job_id:
                since = self._lookup_completed_at(base_job_id)
            include_files = self._collect_files(since=since)
            files_tar = stage / "filestore.tar"
            self._tar_files(include_files, files_tar)
            if files_tar.exists() and files_tar.stat().st_size:
                manifest["files"].append({
                    "kind": "filestore",
                    "name": "filestore.tar",
                    "sha256": _sha256_file(files_tar),
                    "size": files_tar.stat().st_size,
                    "count": len(include_files),
                    "since": since.isoformat() if since else None,
                })

            # 3. Manifest JSON
            mf_path = stage / "manifest.json"
            mf_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

            # 4. Pack everything into a single tar
            archive_name = (
                f"dr_{kind}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
                f"_{uuid.uuid4().hex[:8]}.tar"
            )
            tar_path = stage / archive_name
            with tarfile.open(tar_path, "w") as tar:
                tar.add(mf_path, arcname="manifest.json")
                if db_copy.exists():
                    tar.add(db_copy, arcname="app.db")
                if files_tar.exists() and files_tar.stat().st_size:
                    tar.add(files_tar, arcname="filestore.tar")

            # 5. Stream-compress + encrypt
            final_dir = self._data_dir / "dr" / "archives"
            final_dir.mkdir(parents=True, exist_ok=True)
            suffix = ".tar.gz.aead" if encrypt else ".tar.gz"
            final_path = final_dir / (archive_name + suffix)
            with tar_path.open("rb") as src, final_path.open("wb") as dst:
                used_encrypt, key_id = dr_crypto.encrypt_stream(src, dst, encrypt=encrypt)
            final_sha = _sha256_file(final_path)
            return BackupResult(
                job_id="",
                path=final_path,
                size_bytes=final_path.stat().st_size,
                sha256=final_sha,
                encrypted=used_encrypt,
                key_id=key_id,
                duration_sec=0.0,
                destination=None,
                manifest=manifest,
            )

    def _sqlite_online_backup(self, src_path: Path, dst_path: Path) -> None:
        """Use SQLite's online backup API for a consistent copy."""
        src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
        try:
            dst = sqlite3.connect(str(dst_path))
            try:
                src.backup(dst, pages=-1, progress=None)
            finally:
                dst.close()
        finally:
            src.close()

    def _collect_files(self, since: Optional[datetime]) -> List[Path]:
        out: List[Path] = []
        roots = [p for p in (self._upload_dir,) if p.exists()]
        since_ts = since.timestamp() if since else None
        for root in roots:
            for p in root.rglob("*"):
                if not p.is_file():
                    continue
                if since_ts and p.stat().st_mtime <= since_ts:
                    continue
                out.append(p)
        return out

    def _tar_files(self, files: List[Path], dst: Path) -> None:
        if not files:
            # still create an empty marker so manifest stays consistent
            dst.write_bytes(b"")
            return
        with tarfile.open(dst, "w") as tar:
            for f in files:
                try:
                    arcname = f.relative_to(self._project_root)
                except ValueError:
                    arcname = Path(f.name)
                try:
                    tar.add(f, arcname=str(arcname))
                except OSError as e:
                    logger.warning("dr_backup_file_skip", path=str(f), error=str(e))

    def _lookup_completed_at(self, base_job_id: str) -> Optional[datetime]:
        async def _q():
            async with async_session_factory() as db:
                row = (await db.execute(
                    select(BackupJob).where(BackupJob.id == base_job_id)
                )).scalar_one_or_none()
                return row.completed_at if row else None
        try:
            return asyncio.run(_q())
        except RuntimeError:
            # we are inside a running loop (caller is async) — fall back to sync sqlite
            try:
                with sqlite3.connect(str(self._db_path)) as cx:
                    cur = cx.execute(
                        "SELECT completed_at FROM dr_backup_jobs WHERE id = ?",
                        (base_job_id,),
                    )
                    row = cur.fetchone()
                    if row and row[0]:
                        return datetime.fromisoformat(str(row[0]))
            except Exception:
                pass
        return None


# Singleton — endpoints + scheduler import the same instance.
backup_engine = BackupEngine()
