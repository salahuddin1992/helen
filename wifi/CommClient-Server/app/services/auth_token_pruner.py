"""Auth refresh-token pruner — periodic cleanup of expired/revoked rows.

Refresh tokens accumulate in the DB over time. When they expire or
get revoked (logout, password change), the rows stay around as a
forensic trail. After ``HELEN_AUTH_PRUNE_MAX_AGE_DAYS`` they're no
longer needed for audit and can be removed.

Runs every ``HELEN_AUTH_PRUNE_INTERVAL_SEC`` (default 6 hours) under
a cluster-wide lock so only one peer prunes at a time.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Optional

from app.core.logging import get_logger

logger = get_logger(__name__)


def _f(env: str, default: float) -> float:
    try:
        return float(os.environ.get(env) or default)
    except (TypeError, ValueError):
        return default


def _i(env: str, default: int) -> int:
    try:
        return int(os.environ.get(env) or default)
    except (TypeError, ValueError):
        return default


PRUNE_INTERVAL_SEC = _f("HELEN_AUTH_PRUNE_INTERVAL_SEC", 6 * 3600)
MAX_AGE_DAYS       = _i("HELEN_AUTH_PRUNE_MAX_AGE_DAYS", 90)


_loop_task: Optional[asyncio.Task] = None
_running = False
_pruned_total = 0
_last_run_at: float = 0.0


async def prune_once() -> dict:
    """Delete expired/revoked refresh-token rows beyond max age.

    Wrapped in a cluster-wide lock so only one peer prunes per cycle.
    Returns a stats dict.
    """
    global _pruned_total, _last_run_at
    started = time.time()
    cutoff = time.time() - MAX_AGE_DAYS * 86400.0
    pruned = 0

    try:
        from app.services.distributed_lock import distributed_lock
    except ImportError:
        distributed_lock = None  # type: ignore[assignment]

    async def _do_delete() -> int:
        try:
            from app.db.session import async_session_factory
            from sqlalchemy import text
        except ImportError as e:
            logger.warning("auth_pruner_db_missing", error=str(e))
            return 0
        async with async_session_factory() as db:
            try:
                result = await db.execute(
                    text(
                        "DELETE FROM refresh_tokens "
                        "WHERE (expires_at IS NOT NULL AND expires_at < :cutoff) "
                        "   OR (revoked_at IS NOT NULL AND revoked_at < :cutoff)"
                    ),
                    {"cutoff": cutoff},
                )
                await db.commit()
                rowcount = getattr(result, "rowcount", 0) or 0
                return int(rowcount)
            except Exception as e:
                logger.warning("auth_pruner_delete_failed", error=str(e)[:120])
                try:
                    await db.rollback()
                except Exception:
                    pass
                return 0

    if distributed_lock is not None:
        async with distributed_lock("auth_token_pruner",
                                     ttl=300.0,
                                     acquire_timeout=2.0) as held:
            if not held:
                return {"ran": False, "reason": "lock_held_elsewhere",
                        "elapsed_ms": 0}
            pruned = await _do_delete()
    else:
        pruned = await _do_delete()

    _pruned_total += pruned
    _last_run_at = time.time()
    return {
        "ran":          True,
        "pruned":       pruned,
        "cutoff_unix":  cutoff,
        "max_age_days": MAX_AGE_DAYS,
        "elapsed_ms":   round((time.time() - started) * 1000.0, 2),
    }


async def _run_loop() -> None:
    global _running
    _running = True
    logger.info("auth_token_pruner_started",
                interval_sec=PRUNE_INTERVAL_SEC,
                max_age_days=MAX_AGE_DAYS)
    try:
        while _running:
            try:
                await prune_once()
            except Exception as e:
                logger.warning("auth_pruner_cycle_failed", error=str(e))
            await asyncio.sleep(PRUNE_INTERVAL_SEC)
    finally:
        logger.info("auth_token_pruner_stopped")


def start() -> None:
    global _loop_task
    if _loop_task is not None and not _loop_task.done():
        return
    try:
        loop = asyncio.get_event_loop()
        _loop_task = loop.create_task(_run_loop(), name="auth-token-pruner")
    except RuntimeError:
        logger.warning("auth_token_pruner_no_event_loop_yet")


def stop() -> None:
    global _running, _loop_task
    _running = False
    if _loop_task is not None:
        _loop_task.cancel()
        _loop_task = None


def status() -> dict:
    return {
        "running":         _running,
        "interval_sec":    PRUNE_INTERVAL_SEC,
        "max_age_days":    MAX_AGE_DAYS,
        "pruned_total":    _pruned_total,
        "last_run_at":     _last_run_at,
    }
