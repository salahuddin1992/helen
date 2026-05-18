"""
Phase 6 / Module AA — restore engine.

Workflow
--------
1.  ``verify_restorability(restore_point_id)`` — quick integrity check
    (sha256 + manifest decode) without touching the live DB.
2.  ``simulate_restore(restore_point_id)`` — perform the full restore
    against a sandboxed temp directory. Used by DR drills.
3.  ``restore_full(restore_point_id, dry_run=True)`` — produce a plan
    against the live DB without applying changes.
4.  ``restore_full(restore_point_id, dry_run=False, confirmation_token=…)``
    — actually swap the SQLite file and restore the file store.
5.  ``restore_to_point_in_time(timestamp)`` — find the nearest restore
    point ≤ ``timestamp`` and apply it.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import secrets
import shutil
import sqlite3
import tarfile
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import desc, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.session import async_session_factory
from app.models.dr import (
    BackupDestination as DBBackupDestination,
    BackupJob,
    RestoreOperation,
    RestorePoint,
)
from app.services.dr import encryption as dr_crypto
from app.services.dr.destinations import build_destination

logger = get_logger(__name__)


# ── shapes ──────────────────────────────────────────────────────


@dataclass
class RestorePlan:
    ok: bool
    restore_point_id: str
    backup_job_id: str
    archive_path: Optional[str]
    archive_size: int
    encrypted: bool
    sha256_match: bool
    db_size: int
    file_count: int
    schema_version: str
    app_version: str
    issues: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "restore_point_id": self.restore_point_id,
            "backup_job_id": self.backup_job_id,
            "archive_path": self.archive_path,
            "archive_size": self.archive_size,
            "encrypted": self.encrypted,
            "sha256_match": self.sha256_match,
            "db_size": self.db_size,
            "file_count": self.file_count,
            "schema_version": self.schema_version,
            "app_version": self.app_version,
            "issues": list(self.issues),
        }


# ── engine ──────────────────────────────────────────────────────


class RestoreEngine:
    def __init__(self) -> None:
        s = get_settings()
        self._project_root = Path(getattr(s, "PROJECT_ROOT", "."))
        self._db_path = Path(getattr(s, "SQLITE_PATH", "data/app.db"))
        if not self._db_path.is_absolute():
            self._db_path = (self._project_root / self._db_path).resolve()
        self._upload_dir = Path(getattr(s, "UPLOAD_DIR", "data/uploads"))
        if not self._upload_dir.is_absolute():
            self._upload_dir = (self._project_root / self._upload_dir).resolve()
        self._scratch = (self._project_root / "data" / "dr" / "restore").resolve()
        self._scratch.mkdir(parents=True, exist_ok=True)
        self._tokens: Dict[str, str] = {}        # restore_op_id -> token

    # ── public ──────────────────────────────────────────────────

    async def verify_restorability(self, restore_point_id: str) -> RestorePlan:
        return await asyncio.to_thread(self._build_plan, restore_point_id_sync=restore_point_id, do_full=False)

    async def simulate_restore(self, restore_point_id: str) -> Dict[str, Any]:
        return await asyncio.to_thread(self._simulate, restore_point_id)

    async def restore_full(
        self,
        restore_point_id: str,
        *,
        initiated_by: str,
        dry_run: bool = True,
        confirmation_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        op_id = uuid.uuid4().hex
        async with async_session_factory() as db:
            op = RestoreOperation(
                id=op_id,
                restore_point_id=restore_point_id,
                initiated_by=initiated_by,
                status="verifying",
                dry_run=dry_run,
            )
            db.add(op)
            await db.commit()

        try:
            plan = await asyncio.to_thread(
                self._build_plan, restore_point_id_sync=restore_point_id, do_full=False,
            )
            report: Dict[str, Any] = {"plan": plan.as_dict()}

            if dry_run:
                token = secrets.token_urlsafe(24)
                self._tokens[op_id] = token
                report["confirmation_token"] = token
                async with async_session_factory() as db:
                    await db.execute(
                        update(RestoreOperation).where(RestoreOperation.id == op_id).values(
                            status="succeeded",
                            completed_at=datetime.now(timezone.utc),
                            report=report,
                            confirmation_token=token,
                        )
                    )
                    await db.commit()
                return {"operation_id": op_id, "dry_run": True, "report": report}

            if confirmation_token != self._tokens.get(op_id) and confirmation_token != self._lookup_token(op_id):
                # also accept token from any recent dry-run report on same restore point
                if not await self._token_matches_any_recent(restore_point_id, confirmation_token):
                    raise PermissionError("confirmation token invalid or missing")

            async with async_session_factory() as db:
                await db.execute(
                    update(RestoreOperation).where(RestoreOperation.id == op_id).values(
                        status="restoring",
                    )
                )
                await db.commit()

            apply_report = await asyncio.to_thread(
                self._apply_restore, restore_point_id, sandbox=False,
            )
            report["apply"] = apply_report
            async with async_session_factory() as db:
                await db.execute(
                    update(RestoreOperation).where(RestoreOperation.id == op_id).values(
                        status="succeeded",
                        completed_at=datetime.now(timezone.utc),
                        report=report,
                    )
                )
                await db.commit()
            return {"operation_id": op_id, "dry_run": False, "report": report}

        except Exception as e:
            logger.exception("dr_restore_failed", op_id=op_id, error=str(e))
            async with async_session_factory() as db:
                await db.execute(
                    update(RestoreOperation).where(RestoreOperation.id == op_id).values(
                        status="failed",
                        completed_at=datetime.now(timezone.utc),
                        error_message=str(e)[:2000],
                    )
                )
                await db.commit()
            raise

    async def restore_to_point_in_time(
        self,
        ts: datetime,
        *,
        initiated_by: str,
        dry_run: bool = True,
    ) -> Dict[str, Any]:
        async with async_session_factory() as db:
            row = (await db.execute(
                select(RestorePoint).where(RestorePoint.created_at <= ts)
                .order_by(desc(RestorePoint.created_at)).limit(1)
            )).scalar_one_or_none()
        if row is None:
            raise LookupError(f"no restore point earlier than {ts.isoformat()}")
        return await self.restore_full(
            row.id, initiated_by=initiated_by, dry_run=dry_run,
        )

    # ── private ─────────────────────────────────────────────────

    def _lookup_token(self, op_id: str) -> Optional[str]:
        try:
            with sqlite3.connect(str(self._db_path)) as cx:
                cur = cx.execute(
                    "SELECT confirmation_token FROM dr_restore_operations WHERE id = ?",
                    (op_id,),
                )
                row = cur.fetchone()
                if row and row[0]:
                    return str(row[0])
        except Exception:
            pass
        return None

    async def _token_matches_any_recent(self, restore_point_id: str, token: Optional[str]) -> bool:
        if not token:
            return False
        async with async_session_factory() as db:
            rows = (await db.execute(
                select(RestoreOperation).where(
                    RestoreOperation.restore_point_id == restore_point_id,
                    RestoreOperation.confirmation_token == token,
                    RestoreOperation.dry_run.is_(True),
                ).limit(1)
            )).scalars().all()
        return bool(rows)

    def _build_plan(self, *, restore_point_id_sync: str, do_full: bool) -> RestorePlan:
        with sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True) as cx:
            cur = cx.execute(
                "SELECT rp.id, rp.backup_job_id, rp.schema_version, rp.app_version, "
                "       rp.manifest, j.destination, j.size_bytes, j.sha256, j.encrypted "
                "FROM dr_restore_points rp "
                "JOIN dr_backup_jobs j ON j.id = rp.backup_job_id "
                "WHERE rp.id = ?",
                (restore_point_id_sync,),
            )
            row = cur.fetchone()
        if not row:
            return RestorePlan(
                ok=False, restore_point_id=restore_point_id_sync, backup_job_id="",
                archive_path=None, archive_size=0, encrypted=False,
                sha256_match=False, db_size=0, file_count=0,
                schema_version="", app_version="",
                issues=["restore point not found"],
            )
        (rp_id, job_id, schema_ver, app_ver, manifest_raw, dest, size, sha, enc) = row
        issues: List[str] = []
        try:
            manifest = json.loads(manifest_raw) if isinstance(manifest_raw, str) else (manifest_raw or {})
        except Exception:
            manifest = {}
            issues.append("manifest unparseable")
        archive_path = dest if dest and Path(dest).exists() else None
        sha256_match = False
        archive_size = 0
        if archive_path:
            archive_size = Path(archive_path).stat().st_size
            if sha:
                h = hashlib.sha256()
                with open(archive_path, "rb") as f:
                    while True:
                        b = f.read(1024 * 1024)
                        if not b:
                            break
                        h.update(b)
                sha256_match = (h.hexdigest() == sha)
                if not sha256_match:
                    issues.append("archive sha256 mismatch")
        else:
            issues.append("archive missing on local disk")

        db_size = 0
        file_count = 0
        for entry in (manifest.get("files") or []):
            if entry.get("kind") == "sqlite":
                db_size = int(entry.get("size") or 0)
            elif entry.get("kind") == "filestore":
                file_count = int(entry.get("count") or 0)

        ok = sha256_match and bool(archive_path)
        return RestorePlan(
            ok=ok, restore_point_id=rp_id, backup_job_id=job_id,
            archive_path=archive_path, archive_size=archive_size,
            encrypted=bool(enc), sha256_match=sha256_match,
            db_size=db_size, file_count=file_count,
            schema_version=str(schema_ver or ""), app_version=str(app_ver or ""),
            issues=issues,
        )

    def _simulate(self, restore_point_id: str) -> Dict[str, Any]:
        t0 = datetime.now(timezone.utc)
        plan = self._build_plan(restore_point_id_sync=restore_point_id, do_full=False)
        if not plan.archive_path:
            return {"ok": False, "error": "archive missing", "plan": plan.as_dict()}
        with tempfile.TemporaryDirectory(prefix="dr_sim_", dir=str(self._scratch)) as td:
            sandbox = Path(td)
            extracted = self._unpack_archive(Path(plan.archive_path), sandbox)
            db_ok = True
            integrity = ""
            if (extracted / "app.db").exists():
                try:
                    with sqlite3.connect(str(extracted / "app.db")) as cx:
                        integrity = str(cx.execute("PRAGMA integrity_check").fetchone()[0])
                        db_ok = (integrity == "ok")
                except Exception as e:
                    db_ok = False
                    integrity = f"sqlite error: {e}"
            t1 = datetime.now(timezone.utc)
            return {
                "ok": plan.ok and db_ok,
                "rto_seconds": int((t1 - t0).total_seconds()),
                "sandbox": str(sandbox),
                "plan": plan.as_dict(),
                "integrity": integrity,
            }

    def _apply_restore(self, restore_point_id: str, sandbox: bool) -> Dict[str, Any]:
        plan = self._build_plan(restore_point_id_sync=restore_point_id, do_full=True)
        if not plan.archive_path:
            raise FileNotFoundError("archive missing")
        if not plan.sha256_match:
            raise RuntimeError("sha256 mismatch — refusing to restore")

        with tempfile.TemporaryDirectory(prefix="dr_apply_", dir=str(self._scratch)) as td:
            staged = self._unpack_archive(Path(plan.archive_path), Path(td))

            # 1. swap DB atomically
            new_db = staged / "app.db"
            if new_db.exists():
                bak_path = self._db_path.with_suffix(self._db_path.suffix + f".pre-restore-{int(datetime.now().timestamp())}")
                if self._db_path.exists():
                    shutil.move(str(self._db_path), str(bak_path))
                shutil.move(str(new_db), str(self._db_path))

            # 2. extract filestore
            fs_tar = staged / "filestore.tar"
            restored_files = 0
            if fs_tar.exists() and fs_tar.stat().st_size > 0:
                with tarfile.open(fs_tar, "r") as t:
                    for member in t.getmembers():
                        if not member.isfile():
                            continue
                        target = self._project_root / member.name
                        target.parent.mkdir(parents=True, exist_ok=True)
                        ef = t.extractfile(member)
                        if ef:
                            with target.open("wb") as out:
                                shutil.copyfileobj(ef, out)
                            restored_files += 1
            return {
                "db_restored": new_db.exists() is False and self._db_path.exists(),
                "files_restored": restored_files,
                "applied_at": datetime.now(timezone.utc).isoformat(),
            }

    def _unpack_archive(self, archive: Path, dest: Path) -> Path:
        dest.mkdir(parents=True, exist_ok=True)
        # encrypted? sniff magic
        magic_buf = archive.open("rb")
        try:
            header = magic_buf.read(4)
        finally:
            magic_buf.close()
        plain_tar = dest / "inner.tar"
        with archive.open("rb") as src, plain_tar.open("wb") as out:
            dr_crypto.decrypt_stream(src, out)
        with tarfile.open(plain_tar, "r") as t:
            t.extractall(dest)
        try:
            plain_tar.unlink()
        except OSError:
            pass
        return dest


restore_engine = RestoreEngine()
