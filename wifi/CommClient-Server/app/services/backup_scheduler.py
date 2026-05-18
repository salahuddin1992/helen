"""
Automated backup scheduler — periodic snapshots + retention.

Runs as an asyncio task spawned from the FastAPI lifespan. On each tick it
creates a fresh backup via `backup_service` and prunes the directory down
to `AUTO_BACKUP_RETAIN_COUNT` files.

The scheduler is intentionally boring:
  * single background task — no process forking, no extra threads
  * failures are logged but never re-raised — a bad disk shouldn't crash
    the whole server
  * exposes `last_result` so the admin stats endpoint can surface
    "last backup: 2h ago — OK"

Disable via `AUTO_BACKUP_ENABLED=false`.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from app.core.config import get_settings
from app.core.logging import get_logger
from app.services.backup_service import backup_service

logger = get_logger(__name__)


@dataclass
class BackupRunResult:
    ts: datetime
    ok: bool
    backup_name: Optional[str] = None
    pruned: int = 0
    error: Optional[str] = None
    verified: Optional[bool] = None          # None = not attempted
    verify_error: Optional[str] = None
    page_count: int = 0
    size_bytes: int = 0


@dataclass
class BackupSchedulerState:
    """Observable state for admin dashboards."""
    enabled: bool = False
    interval_hours: float = 0.0
    retain_count: int = 0
    last_run: Optional[BackupRunResult] = None
    run_count: int = 0
    failure_count: int = 0
    history: list[BackupRunResult] = field(default_factory=list)  # last ~20

    def snapshot(self) -> dict:
        return {
            "enabled": self.enabled,
            "interval_hours": self.interval_hours,
            "retain_count": self.retain_count,
            "run_count": self.run_count,
            "failure_count": self.failure_count,
            "last_run": {
                "ts": self.last_run.ts.isoformat() if self.last_run else None,
                "ok": self.last_run.ok if self.last_run else None,
                "backup_name": self.last_run.backup_name if self.last_run else None,
                "pruned": self.last_run.pruned if self.last_run else 0,
                "error": self.last_run.error if self.last_run else None,
                "verified": self.last_run.verified if self.last_run else None,
                "verify_error": self.last_run.verify_error if self.last_run else None,
                "page_count": self.last_run.page_count if self.last_run else 0,
                "size_bytes": self.last_run.size_bytes if self.last_run else 0,
            } if self.last_run else None,
        }


_state = BackupSchedulerState()
_task: Optional[asyncio.Task] = None


def get_state() -> BackupSchedulerState:
    return _state


async def _run_once() -> BackupRunResult:
    """Execute one backup + verify + retention pass.

    A run is only "ok" if both create AND verify succeed — a corrupted
    snapshot is worse than no snapshot because operators trust it exists.
    """
    settings = get_settings()
    ts = datetime.now(timezone.utc)
    try:
        name = await backup_service.create_backup()
    except Exception as e:
        _state.failure_count += 1
        res = BackupRunResult(ts=ts, ok=False, error=str(e))
        logger.error("auto_backup_create_failed", error=str(e))
        return res

    # Verify the freshly-written snapshot before we trust it enough to
    # prune older copies. A failed verify means we KEEP the old backups.
    verified: Optional[bool] = None
    verify_error: Optional[str] = None
    page_count = 0
    size_bytes = 0
    try:
        vr = await backup_service.verify_backup(name)
        verified = bool(vr.get("ok"))
        verify_error = vr.get("error") if not verified else None
        page_count = int(vr.get("page_count") or 0)
        size_bytes = int(vr.get("size_bytes") or 0)
    except Exception as e:
        verified = False
        verify_error = f"verify_raised: {e}"
        logger.error("auto_backup_verify_raised", error=str(e), backup_name=name)

    if verified is False:
        # Do NOT prune on failed verify — operator needs older good copies.
        _state.failure_count += 1
        logger.error(
            "auto_backup_verify_failed_keeping_history",
            backup_name=name, error=verify_error,
        )
        return BackupRunResult(
            ts=ts, ok=False, backup_name=name, pruned=0,
            error=f"verify_failed: {verify_error}",
            verified=False, verify_error=verify_error,
            page_count=page_count, size_bytes=size_bytes,
        )

    pruned = 0
    try:
        pruned = await backup_service.auto_cleanup(
            keep_count=max(1, int(settings.AUTO_BACKUP_RETAIN_COUNT)),
        )
    except Exception as e:
        # Cleanup failure is less severe than create failure — we still
        # succeeded in creating the snapshot, just didn't prune.
        logger.warning("auto_backup_cleanup_failed", error=str(e))

    logger.info(
        "auto_backup_ok", backup_name=name, pruned=pruned,
        verified=verified, page_count=page_count, size_bytes=size_bytes,
    )
    return BackupRunResult(
        ts=ts, ok=True, backup_name=name, pruned=pruned,
        verified=verified, page_count=page_count, size_bytes=size_bytes,
    )


async def _scheduler_loop() -> None:
    settings = get_settings()
    # Let the server finish warming before we contend on the SQLite file.
    await asyncio.sleep(max(0, int(settings.AUTO_BACKUP_STARTUP_DELAY_SEC)))

    interval_sec = max(60.0, float(settings.AUTO_BACKUP_INTERVAL_HOURS) * 3600.0)

    while True:
        try:
            res = await _run_once()
            _state.run_count += 1
            _state.last_run = res
            _state.history.append(res)
            if len(_state.history) > 20:
                _state.history = _state.history[-20:]
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # Catch-all — the loop must survive any subsystem hiccup.
            logger.exception("auto_backup_loop_unexpected", exc_info=e)

        try:
            await asyncio.sleep(interval_sec)
        except asyncio.CancelledError:
            raise


async def start() -> None:
    """Launch the scheduler if enabled. Idempotent."""
    global _task
    settings = get_settings()
    _state.enabled = bool(settings.AUTO_BACKUP_ENABLED)
    _state.interval_hours = float(settings.AUTO_BACKUP_INTERVAL_HOURS)
    _state.retain_count = int(settings.AUTO_BACKUP_RETAIN_COUNT)

    if not _state.enabled:
        logger.info("auto_backup_disabled")
        return
    if _task and not _task.done():
        return

    _task = asyncio.create_task(_scheduler_loop(), name="auto_backup_scheduler")
    logger.info(
        "auto_backup_scheduler_started",
        interval_hours=_state.interval_hours,
        retain_count=_state.retain_count,
    )


async def stop() -> None:
    """Cancel the scheduler cleanly (on app shutdown)."""
    global _task
    if _task and not _task.done():
        _task.cancel()
        try:
            await _task
        except (asyncio.CancelledError, Exception):
            pass
    _task = None


async def trigger_now() -> BackupRunResult:
    """Run a backup immediately (admin-triggered). Does not affect the timer."""
    res = await _run_once()
    _state.run_count += 1
    _state.last_run = res
    _state.history.append(res)
    if len(_state.history) > 20:
        _state.history = _state.history[-20:]
    return res
