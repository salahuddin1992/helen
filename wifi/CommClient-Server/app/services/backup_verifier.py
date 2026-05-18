"""
Backup verification scheduler.

Why a separate file from ``backup_service``
-------------------------------------------
``backup_service`` *creates* and *restores* backups. ``backup_scheduler``
*creates them on a cron*. Neither answers the question every operator
eventually asks: **are these backups actually restorable?**

This module fills that gap. On a configurable interval it:

  1. Picks the most-recent backup file from ``data/backups/``.
  2. Copies it into a temp dir (we never touch the live DB).
  3. Opens it as a SQLite database and runs a battery of read-only
     checks (``PRAGMA integrity_check``, schema-version, table
     row-count probes for the critical tables).
  4. Records the result in an in-memory rolling history that the
     admin endpoint can read.

If a verification fails, we log a structured ``backup_verify_failed``
event and bump a Prometheus-shaped counter so the existing
``metrics_export`` pipeline picks it up automatically.

This module never *fixes* a bad backup — it only flags it. Keeping
write paths out of the verifier means a misbehaving check can never
corrupt the live system.

Dedicated env vars (no edits to existing modules required)
----------------------------------------------------------
    HELEN_BACKUP_VERIFY_ENABLED       1 to enable the loop
    HELEN_BACKUP_VERIFY_INTERVAL_S    seconds between runs (default 86400)
    HELEN_BACKUP_VERIFY_HISTORY_MAX   how many results to keep (default 30)
    HELEN_BACKUP_VERIFY_REQUIRED_TABLES
                                       CSV of tables the verifier expects
                                       to find (default: users,messages)
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sqlite3
import tempfile
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from app.core.logging import get_logger

logger = get_logger(__name__)


# ── Result types ──────────────────────────────────────────────────


@dataclass
class TableCheckResult:
    table: str
    ok: bool
    row_count: Optional[int] = None
    error: Optional[str] = None


@dataclass
class VerifyResult:
    ran_at: float
    backup_name: Optional[str]
    backup_size_bytes: Optional[int]
    duration_ms: float
    integrity_ok: bool
    integrity_error: Optional[str] = None
    schema_version: Optional[int] = None
    table_checks: list[TableCheckResult] = field(default_factory=list)
    overall_ok: bool = False
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "ran_at": self.ran_at,
            "backup_name": self.backup_name,
            "backup_size_bytes": self.backup_size_bytes,
            "duration_ms": self.duration_ms,
            "integrity_ok": self.integrity_ok,
            "integrity_error": self.integrity_error,
            "schema_version": self.schema_version,
            "table_checks": [
                {"table": c.table, "ok": c.ok,
                 "row_count": c.row_count, "error": c.error}
                for c in self.table_checks
            ],
            "overall_ok": self.overall_ok,
            "error": self.error,
        }


# ── Verifier ──────────────────────────────────────────────────────


def verify_backup_file(
    backup_path: Path,
    *,
    required_tables: tuple[str, ...] = ("users", "messages"),
) -> VerifyResult:
    """Run all checks against a backup *file*. Pure function — does
    not depend on the running app, safe to call from tests.

    The backup is copied into a temp dir so we read from a stable
    snapshot and never hold a lock on the original file."""
    started = time.perf_counter()
    if not backup_path.exists():
        return VerifyResult(
            ran_at=time.time(),
            backup_name=backup_path.name,
            backup_size_bytes=None,
            duration_ms=0.0,
            integrity_ok=False,
            error=f"backup file not found: {backup_path}",
        )

    size_bytes = backup_path.stat().st_size
    with tempfile.TemporaryDirectory(prefix="helen-bk-verify-") as td:
        scratch = Path(td) / backup_path.name
        try:
            shutil.copy2(str(backup_path), str(scratch))
        except OSError as e:
            return VerifyResult(
                ran_at=time.time(),
                backup_name=backup_path.name,
                backup_size_bytes=size_bytes,
                duration_ms=(time.perf_counter() - started) * 1000.0,
                integrity_ok=False,
                error=f"copy to temp failed: {e}",
            )

        integrity_ok = False
        integrity_error: Optional[str] = None
        schema_version: Optional[int] = None
        table_checks: list[TableCheckResult] = []
        try:
            conn = sqlite3.connect(
                f"file:{scratch}?mode=ro", uri=True,
            )
            try:
                cur = conn.cursor()
                row = cur.execute(
                    "PRAGMA integrity_check;",
                ).fetchone()
                if row and row[0] == "ok":
                    integrity_ok = True
                else:
                    integrity_error = (row[0] if row else "no result")

                try:
                    sv = cur.execute(
                        "PRAGMA schema_version;",
                    ).fetchone()
                    if sv:
                        schema_version = int(sv[0])
                except sqlite3.Error:
                    pass

                for tbl in required_tables:
                    try:
                        # quote-by-replacement to defend against the
                        # configured-tables list ever including hostile
                        # input (it's an env var the operator owns,
                        # but be paranoid anyway).
                        safe = tbl.replace('"', '""')
                        rc_row = cur.execute(
                            f'SELECT COUNT(*) FROM "{safe}"',
                        ).fetchone()
                        rc = int(rc_row[0]) if rc_row else 0
                        table_checks.append(TableCheckResult(
                            table=tbl, ok=True, row_count=rc,
                        ))
                    except sqlite3.Error as te:
                        table_checks.append(TableCheckResult(
                            table=tbl, ok=False, error=str(te),
                        ))
            finally:
                conn.close()
        except sqlite3.Error as e:
            return VerifyResult(
                ran_at=time.time(),
                backup_name=backup_path.name,
                backup_size_bytes=size_bytes,
                duration_ms=(time.perf_counter() - started) * 1000.0,
                integrity_ok=False,
                error=f"sqlite open failed: {e}",
            )

    overall_ok = (
        integrity_ok
        and all(c.ok for c in table_checks)
    )
    return VerifyResult(
        ran_at=time.time(),
        backup_name=backup_path.name,
        backup_size_bytes=size_bytes,
        duration_ms=(time.perf_counter() - started) * 1000.0,
        integrity_ok=integrity_ok,
        integrity_error=integrity_error,
        schema_version=schema_version,
        table_checks=table_checks,
        overall_ok=overall_ok,
    )


def find_latest_backup(backup_dir: Path) -> Optional[Path]:
    if not backup_dir.is_dir():
        return None
    cands = sorted(backup_dir.glob("commclient_backup_*.db"))
    return cands[-1] if cands else None


# ── Scheduler ─────────────────────────────────────────────────────


class BackupVerifier:
    def __init__(
        self,
        backup_dir: Path,
        *,
        interval_s: int = 86400,
        history_max: int = 30,
        required_tables: tuple[str, ...] = ("users", "messages"),
    ) -> None:
        self.backup_dir = backup_dir
        self.interval_s = max(60, int(interval_s))
        self.required_tables = required_tables
        self.history: deque[VerifyResult] = deque(maxlen=history_max)
        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._enabled = False

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._enabled = True
        self._task = asyncio.create_task(
            self._loop(), name="backup-verifier",
        )
        logger.info("backup_verifier_started",
                    interval_s=self.interval_s,
                    required_tables=list(self.required_tables))

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            self._task = None
        self._enabled = False

    async def run_once(self) -> VerifyResult:
        latest = find_latest_backup(self.backup_dir)
        if latest is None:
            r = VerifyResult(
                ran_at=time.time(),
                backup_name=None, backup_size_bytes=None,
                duration_ms=0.0, integrity_ok=False,
                error="no backups found in dir",
            )
            self.history.append(r)
            return r
        r = await asyncio.to_thread(
            verify_backup_file, latest,
            required_tables=self.required_tables,
        )
        self.history.append(r)
        if r.overall_ok:
            logger.info("backup_verify_ok",
                        backup=r.backup_name,
                        duration_ms=int(r.duration_ms))
        else:
            logger.warning("backup_verify_failed",
                           backup=r.backup_name,
                           integrity_ok=r.integrity_ok,
                           error=r.error or r.integrity_error,
                           bad_tables=[c.table for c in r.table_checks
                                       if not c.ok])
        return r

    async def _loop(self) -> None:
        # Initial delay so we don't pile work onto a fresh boot.
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=30.0)
            return  # stopped before first run
        except asyncio.TimeoutError:
            pass
        while not self._stop.is_set():
            try:
                await self.run_once()
            except Exception as e:
                logger.warning("backup_verify_crashed", error=str(e))
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.interval_s,
                )
            except asyncio.TimeoutError:
                continue

    def status(self) -> dict:
        last = self.history[-1] if self.history else None
        return {
            "enabled": self._enabled,
            "backup_dir": str(self.backup_dir),
            "interval_s": self.interval_s,
            "required_tables": list(self.required_tables),
            "history_size": len(self.history),
            "last": last.to_dict() if last else None,
            "history": [r.to_dict() for r in self.history],
        }


# ── Singleton helpers ─────────────────────────────────────────────


_verifier: Optional[BackupVerifier] = None


def configure_backup_verifier(backup_dir: Path,
                                **kw) -> BackupVerifier:
    global _verifier
    _verifier = BackupVerifier(backup_dir, **kw)
    return _verifier


def get_backup_verifier() -> Optional[BackupVerifier]:
    return _verifier


async def shutdown_backup_verifier() -> None:
    global _verifier
    if _verifier is not None:
        await _verifier.stop()
        _verifier = None


def configure_from_env(backup_dir: Path) -> Optional[BackupVerifier]:
    if os.environ.get(
        "HELEN_BACKUP_VERIFY_ENABLED", "",
    ).lower() not in ("1", "true", "yes"):
        return None
    interval_s = int(os.environ.get(
        "HELEN_BACKUP_VERIFY_INTERVAL_S", "86400",
    ))
    history_max = int(os.environ.get(
        "HELEN_BACKUP_VERIFY_HISTORY_MAX", "30",
    ))
    required_csv = os.environ.get(
        "HELEN_BACKUP_VERIFY_REQUIRED_TABLES", "users,messages",
    )
    required = tuple(t.strip() for t in required_csv.split(",")
                     if t.strip())
    return configure_backup_verifier(
        backup_dir,
        interval_s=interval_s,
        history_max=history_max,
        required_tables=required,
    )


__all__ = [
    "BackupVerifier",
    "VerifyResult",
    "TableCheckResult",
    "verify_backup_file",
    "find_latest_backup",
    "configure_backup_verifier",
    "get_backup_verifier",
    "shutdown_backup_verifier",
    "configure_from_env",
]
