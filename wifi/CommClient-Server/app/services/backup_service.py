"""
Database backup and restore service for SQLite.
Handles creating timestamped backups, listing, restoring, and auto-cleanup.
Backups are stored in data/backups/ with rotation policy (keep N recent).

CAUTION: restore_backup() overwrites the active database and should be preceded
by an audit log entry.
"""

from __future__ import annotations

import asyncio
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class BackupService:
    """Manages SQLite database backups with rotation and cleanup."""

    def __init__(self):
        settings = get_settings()
        self._db_path = Path(settings.SQLITE_PATH)
        # Support absolute paths
        if not self._db_path.is_absolute():
            self._db_path = (settings.PROJECT_ROOT / self._db_path).resolve()

        self._backup_dir = settings.PROJECT_ROOT / "data" / "backups"
        self._backup_dir.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()

        logger.info(
            "backup_service_init",
            db_path=str(self._db_path),
            backup_dir=str(self._backup_dir),
        )

    async def create_backup(self) -> str:
        """
        Create a timestamped backup of the SQLite database.
        Uses SQLite's online backup API so the copy is consistent even
        when writers are active — a plain shutil.copy can grab a half-
        flushed page in WAL mode and produce a backup that fails
        ``PRAGMA integrity_check``.
        Returns: Path to the backup file (relative to backup_dir).
        Raises: OSError if the database or backup directory is inaccessible.
        """
        async with self._lock:
            if not self._db_path.exists():
                logger.error("backup_failed_db_missing", db_path=str(self._db_path))
                raise FileNotFoundError(f"Database not found at {self._db_path}")

            now = datetime.now(timezone.utc)
            timestamp = now.strftime("%Y%m%d_%H%M%S")
            backup_name = f"commclient_backup_{timestamp}.db"
            backup_path = self._backup_dir / backup_name

            def _do_backup() -> int:
                src = sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True)
                try:
                    dst = sqlite3.connect(str(backup_path))
                    try:
                        # pages=-1 + sleep=0 → copy in one shot when writer
                        # is idle; falls back to incremental if it isn't.
                        src.backup(dst, pages=-1, progress=None)
                    finally:
                        dst.close()
                finally:
                    src.close()
                return backup_path.stat().st_size

            try:
                size_bytes = await asyncio.to_thread(_do_backup)
                logger.info(
                    "backup_created",
                    backup_name=backup_name,
                    size_bytes=size_bytes,
                )
                return backup_name
            except sqlite3.Error as e:
                logger.error("backup_failed_sqlite", error=str(e), backup_name=backup_name)
                # Clean up partial file so retries don't leave debris.
                try:
                    backup_path.unlink(missing_ok=True)
                except Exception:
                    pass
                raise OSError(f"sqlite backup failed: {e}") from e
            except (IOError, OSError) as e:
                logger.error("backup_failed", error=str(e), backup_name=backup_name)
                raise

    async def list_backups(self) -> list[dict[str, Any]]:
        """
        List all backup files with metadata.
        Returns: List of dicts with keys: name, size_bytes, created_at (ISO string).
        """
        async with self._lock:
            backups = []
            if not self._backup_dir.exists():
                return backups

            for backup_file in sorted(self._backup_dir.glob("commclient_backup_*.db")):
                try:
                    stat = backup_file.stat()
                    backups.append({
                        "name": backup_file.name,
                        "size_bytes": stat.st_size,
                        "created_at": datetime.fromtimestamp(
                            stat.st_ctime, tz=timezone.utc
                        ).isoformat(),
                    })
                except (IOError, OSError) as e:
                    logger.warning("backup_stat_failed", backup_name=backup_file.name, error=str(e))

            return backups

    async def restore_backup(self, backup_name: str) -> bool:
        """
        Restore a specific backup — overwrites the active database.
        DANGEROUS: Should be preceded by creating a protective backup and audit logging.

        Args:
            backup_name: Filename of the backup to restore (e.g., "commclient_backup_20260408_143000.db")

        Returns:
            True if restore succeeded, False otherwise.
        """
        async with self._lock:
            backup_path = self._backup_dir / backup_name

            # Validate backup exists
            if not backup_path.exists():
                logger.error("restore_failed_backup_missing", backup_name=backup_name)
                return False

            # Validate this is a valid backup file (basic check)
            if not backup_path.name.startswith("commclient_backup_"):
                logger.error("restore_failed_invalid_name", backup_name=backup_name)
                return False

            try:
                # Create a protective backup of current DB before restore
                if self._db_path.exists():
                    protective_name = f"commclient_backup_before_restore_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.db"
                    protective_path = self._backup_dir / protective_name
                    shutil.copy2(str(self._db_path), str(protective_path))
                    logger.info("protective_backup_created", backup_name=protective_name)

                # Restore the backup
                shutil.copy2(str(backup_path), str(self._db_path))
                logger.warning("backup_restored", backup_name=backup_name, db_path=str(self._db_path))
                return True

            except (IOError, OSError) as e:
                logger.error("restore_failed", backup_name=backup_name, error=str(e))
                return False

    async def delete_backup(self, backup_name: str) -> bool:
        """
        Delete a specific backup file.
        Validates filename to prevent path traversal attacks.
        """
        async with self._lock:
            backup_path = self._backup_dir / backup_name

            # Validate safe filename (must not contain path separators)
            if not backup_path.name.startswith("commclient_backup_") or ".." in backup_name:
                logger.warning("delete_backup_invalid_name", backup_name=backup_name)
                return False

            if not backup_path.exists():
                logger.warning("delete_backup_not_found", backup_name=backup_name)
                return False

            try:
                backup_path.unlink()
                logger.info("backup_deleted", backup_name=backup_name)
                return True
            except (IOError, OSError) as e:
                logger.error("delete_backup_failed", backup_name=backup_name, error=str(e))
                return False

    async def auto_cleanup(self, keep_count: int = 10) -> int:
        """
        Delete old backups, keeping only the N most recent ones.
        Returns: Number of backups deleted.
        """
        async with self._lock:
            if not self._backup_dir.exists():
                return 0

            # List all backups sorted by creation time (oldest first)
            backup_files = sorted(
                self._backup_dir.glob("commclient_backup_*.db"),
                key=lambda p: p.stat().st_ctime,
            )

            # Keep only the most recent N
            to_delete = backup_files[:-keep_count] if len(backup_files) > keep_count else []
            deleted_count = 0

            for backup_file in to_delete:
                try:
                    backup_file.unlink()
                    deleted_count += 1
                    logger.info("auto_cleanup_deleted", backup_name=backup_file.name)
                except (IOError, OSError) as e:
                    logger.warning("auto_cleanup_failed", backup_name=backup_file.name, error=str(e))

            if deleted_count > 0:
                logger.info("auto_cleanup_completed", deleted_count=deleted_count, kept_count=len(backup_files) - deleted_count)

            return deleted_count

    async def verify_backup(self, backup_name: str) -> dict[str, Any]:
        """
        Verify a backup file's integrity without touching the live DB.

        Opens the backup read-only and runs SQLite's `PRAGMA integrity_check`
        (slow, comprehensive) and `PRAGMA quick_check` (fast, page-level).
        Also samples the schema to confirm expected tables exist — a copy
        that opens but has no `users` table is effectively broken.

        Returns a dict with:
          ok:            bool — all checks passed
          integrity_ok:  bool
          quick_ok:      bool
          schema_ok:     bool — has at least the `users` table
          page_count:    int
          page_size:     int
          size_bytes:    int
          error:         str | None
        """
        backup_path = self._backup_dir / backup_name
        result: dict[str, Any] = {
            "ok": False,
            "integrity_ok": False,
            "quick_ok": False,
            "schema_ok": False,
            "page_count": 0,
            "page_size": 0,
            "size_bytes": 0,
            "error": None,
        }

        if not backup_path.name.startswith("commclient_backup_") or ".." in backup_name:
            result["error"] = "invalid_backup_name"
            return result
        if not backup_path.exists():
            result["error"] = "not_found"
            return result

        try:
            result["size_bytes"] = backup_path.stat().st_size
        except OSError as e:
            result["error"] = f"stat_failed: {e}"
            return result

        # Run checks in a thread — sqlite3 blocks, and we don't want to stall
        # the event loop for multi-second integrity scans on large DBs.
        def _run_checks() -> dict[str, Any]:
            local: dict[str, Any] = {}
            uri = f"file:{backup_path}?mode=ro"
            try:
                conn = sqlite3.connect(uri, uri=True, timeout=10.0)
            except sqlite3.DatabaseError as e:
                local["error"] = f"open_failed: {e}"
                return local
            try:
                cur = conn.cursor()
                try:
                    local["page_count"] = cur.execute("PRAGMA page_count").fetchone()[0]
                    local["page_size"] = cur.execute("PRAGMA page_size").fetchone()[0]
                except sqlite3.DatabaseError as e:
                    local["error"] = f"pragma_failed: {e}"
                    return local

                try:
                    qc = cur.execute("PRAGMA quick_check").fetchone()
                    local["quick_ok"] = bool(qc and qc[0] == "ok")
                except sqlite3.DatabaseError as e:
                    local["error"] = f"quick_check_failed: {e}"
                    return local

                try:
                    # integrity_check returns one row per problem; single
                    # "ok" row = clean database.
                    rows = cur.execute("PRAGMA integrity_check").fetchall()
                    local["integrity_ok"] = len(rows) == 1 and rows[0][0] == "ok"
                    if not local["integrity_ok"]:
                        local["error"] = "integrity_check: " + "; ".join(
                            r[0] for r in rows[:3]
                        )
                except sqlite3.DatabaseError as e:
                    local["error"] = f"integrity_check_failed: {e}"
                    return local

                try:
                    row = cur.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='users' LIMIT 1"
                    ).fetchone()
                    local["schema_ok"] = row is not None
                    if not local["schema_ok"]:
                        local["error"] = local.get("error") or "schema_missing_users_table"
                except sqlite3.DatabaseError as e:
                    local["error"] = f"schema_probe_failed: {e}"
                    return local
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
            return local

        try:
            sub = await asyncio.to_thread(_run_checks)
        except Exception as e:
            result["error"] = f"thread_failed: {e}"
            logger.error("verify_backup_thread_failed", backup_name=backup_name, error=str(e))
            return result

        result.update(sub)
        result["ok"] = bool(
            result["integrity_ok"]
            and result["quick_ok"]
            and result["schema_ok"]
            and not result["error"]
        )
        if result["ok"]:
            logger.info(
                "backup_verified",
                backup_name=backup_name,
                page_count=result["page_count"],
                size_bytes=result["size_bytes"],
            )
        else:
            logger.warning(
                "backup_verify_failed",
                backup_name=backup_name,
                error=result["error"],
                integrity_ok=result["integrity_ok"],
                quick_ok=result["quick_ok"],
                schema_ok=result["schema_ok"],
            )
        return result

    async def get_db_size(self) -> int:
        """
        Return size of the current database file in bytes.
        Returns 0 if the database file doesn't exist.
        """
        try:
            if self._db_path.exists():
                return self._db_path.stat().st_size
        except (IOError, OSError) as e:
            logger.warning("get_db_size_failed", error=str(e))

        return 0


# Singleton instance
backup_service = BackupService()
