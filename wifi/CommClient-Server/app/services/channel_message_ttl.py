"""
Per-channel message TTL — auto-delete old messages.

Operators set a per-channel "messages older than X seconds get
deleted" cap. A background sweeper runs every ``SWEEP_INTERVAL_S``
seconds, walks every configured channel, and removes (soft-delete
where supported, hard-delete otherwise) messages whose
``created_at`` is older than the cap.

Why JSON-backed config (no schema migration)
--------------------------------------------
Same pattern as ``channel_slow_mode.py``: the cap lives in
``$DATA_DIR/channel_message_ttl.json`` so it survives a restart
without forcing a new column on the channels table. The data
plane (the actual DELETE SQL) still touches the messages table —
only the *configuration* is sidecar-stored.

Sweeper cadence
---------------
Default sweep interval is 3600 s (one hour). For a 24h TTL that
means messages live an extra 0..1h beyond the strict deadline,
which is acceptable for a privacy/compliance feature where the
cap is at the day or week level. Operators can crank it lower
via ``HELEN_TTL_SWEEP_INTERVAL_S``.

Wiring
------
``configure_from_env`` is invoked from the lifespan; it spawns
``_sweep_loop`` as a background task. Admin endpoints in
``app/api/routes/channel_message_ttl.py`` set/get the cap.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import delete

from app.core.logging import get_logger

logger = get_logger(__name__)


# ── Config store (JSON-backed, thread-safe) ─────────────────────


class _TTLStore:
    def __init__(self, persist_path: Path) -> None:
        self.persist_path = persist_path
        self._caps: dict[str, int] = {}
        self._lock = threading.Lock()
        self._loaded = False

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self.persist_path.is_file():
            return
        try:
            data = json.loads(self.persist_path.read_text("utf-8"))
            for cid, sec in (data or {}).items():
                self._caps[cid] = int(sec)
        except Exception as e:
            logger.warning("ttl_load_failed",
                           error=str(e), path=str(self.persist_path))

    def _save(self) -> None:
        try:
            self.persist_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                cid: sec for cid, sec in self._caps.items() if sec > 0
            }
            tmp = self.persist_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2), "utf-8")
            tmp.replace(self.persist_path)
        except Exception as e:
            logger.warning("ttl_save_failed",
                           error=str(e), path=str(self.persist_path))

    def get(self, channel_id: str) -> int:
        with self._lock:
            self._load()
            return self._caps.get(channel_id, 0)

    def set(self, channel_id: str, seconds: int) -> int:
        # Cap range: [0 (off), 30 days]. We refuse to retroactively
        # delete *everything* older than 1 second to dodge fat-finger
        # accidents — admins set "an hour" via the UI, not "1".
        seconds = max(0, min(int(seconds), 30 * 24 * 3600))
        if 0 < seconds < 60:
            seconds = 60
        with self._lock:
            self._load()
            if seconds == 0:
                self._caps.pop(channel_id, None)
            else:
                self._caps[channel_id] = seconds
            self._save()
        logger.info("ttl_changed", channel_id=channel_id, seconds=seconds)
        return seconds

    def all_caps(self) -> dict[str, int]:
        with self._lock:
            self._load()
            return dict(self._caps)


_store: Optional[_TTLStore] = None


def _get_store() -> _TTLStore:
    global _store
    if _store is None:
        env_path = os.environ.get("HELEN_TTL_PATH")
        if env_path:
            path = Path(env_path)
        else:
            try:
                from app.core.config import get_settings
                path = (get_settings().PROJECT_ROOT
                        / "data" / "channel_message_ttl.json")
            except Exception:
                path = Path("data/channel_message_ttl.json")
        _store = _TTLStore(path)
    return _store


def get_ttl_seconds(channel_id: str) -> int:
    return _get_store().get(channel_id)


def set_ttl_seconds(channel_id: str, seconds: int) -> int:
    return _get_store().set(channel_id, seconds)


def all_ttl_caps() -> dict[str, int]:
    return _get_store().all_caps()


# ── Background sweeper ───────────────────────────────────────────


async def sweep_once() -> dict:
    """Run one pass over every configured channel. Returns a tiny
    summary dict the admin endpoint can echo back."""
    caps = _get_store().all_caps()
    if not caps:
        return {"channels": 0, "deleted": 0}

    # Lazy import — avoids pulling DB modules into module-load time
    # for installations that haven't enabled this feature.
    try:
        from app.db.session import async_session_maker
        from app.models.message import Message
    except Exception as e:
        logger.warning("ttl_sweep_skip_no_db", error=str(e))
        return {"channels": 0, "deleted": 0, "error": str(e)}

    total_deleted = 0
    now = datetime.now(timezone.utc)

    async with async_session_maker() as db:
        for channel_id, seconds in caps.items():
            cutoff = now - timedelta(seconds=seconds)
            try:
                result = await db.execute(
                    delete(Message).where(
                        Message.channel_id == channel_id,
                        Message.created_at < cutoff,
                    ),
                )
                count = result.rowcount or 0
                total_deleted += count
                if count > 0:
                    logger.info("ttl_sweep_deleted",
                                channel_id=channel_id,
                                deleted=count,
                                seconds=seconds)
            except Exception as e:
                logger.warning("ttl_sweep_channel_failed",
                               channel_id=channel_id, error=str(e))
        try:
            await db.commit()
        except Exception:
            await db.rollback()

    return {"channels": len(caps), "deleted": total_deleted}


_sweep_task: Optional[asyncio.Task] = None
_stop_event: Optional[asyncio.Event] = None


async def _sweep_loop(interval_s: int) -> None:
    assert _stop_event is not None
    # Slight initial delay so we don't pile work onto cold start.
    try:
        await asyncio.wait_for(_stop_event.wait(), timeout=60.0)
        return
    except asyncio.TimeoutError:
        pass
    while not _stop_event.is_set():
        try:
            await sweep_once()
        except Exception as e:
            logger.warning("ttl_sweep_crashed", error=str(e))
        try:
            await asyncio.wait_for(
                _stop_event.wait(), timeout=interval_s,
            )
        except asyncio.TimeoutError:
            continue


def configure_from_env() -> bool:
    """Spawn the sweeper task. Returns True iff the feature is
    enabled (always — the cap dict can be empty, but the sweeper
    is cheap to keep around so future ``set_ttl_seconds`` calls
    take effect without a restart)."""
    global _sweep_task, _stop_event
    if _sweep_task is not None:
        return True
    interval_s = int(os.environ.get(
        "HELEN_TTL_SWEEP_INTERVAL_S", "3600",
    ))
    interval_s = max(60, interval_s)  # never tighter than 60s
    _stop_event = asyncio.Event()
    _sweep_task = asyncio.create_task(
        _sweep_loop(interval_s), name="channel-ttl-sweeper",
    )
    logger.info("ttl_sweeper_started", interval_s=interval_s)
    return True


async def shutdown() -> None:
    global _sweep_task, _stop_event
    if _stop_event:
        _stop_event.set()
    if _sweep_task:
        try:
            await asyncio.wait_for(_sweep_task, timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            _sweep_task.cancel()
    _sweep_task = None
    _stop_event = None


__all__ = [
    "get_ttl_seconds", "set_ttl_seconds", "all_ttl_caps",
    "sweep_once", "configure_from_env", "shutdown",
]
