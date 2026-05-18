"""
Channel slow-mode — per-channel rate limit on message sends.

A "slow-mode" channel rejects messages from a sender that arrive
faster than ``seconds_between_messages`` apart. Each channel admin
sets the cap; admins themselves bypass the limit (they're often
the ones moderating).

Storage
-------
Configuration (per-channel cap) lives in
``$DATA_DIR/channel_slow_mode.json`` so the setting survives a
restart without requiring a DB migration. Loaded once on first
read; saves are atomic via a ``.tmp`` file + replace.

Per-sender last-send timestamps are kept **in memory only** — they
naturally reset across restarts, which is acceptable: slow-mode is
about pacing within a session, not a permanent rate ledger.

Wire-up
-------
``MessageService.send_message`` calls
:func:`check_send_allowed` before persisting; if the result is a
positive number, the request is rejected as
``ChannelSlowModeError(seconds_remaining)`` and the client renders
the countdown.

Dedicated module so the rate-limit logic lives next to the data
and can grow (per-role exemptions, burst windows, etc.) without
bleeding into ``message_service``.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app.core.logging import get_logger

logger = get_logger(__name__)


class ChannelSlowModeError(Exception):
    """Raised by ``check_send_allowed`` when the sender is currently
    rate-limited. ``wait_seconds`` is how long until the next send
    will be accepted (rounded up to whole seconds for the UI)."""

    def __init__(self, wait_seconds: float, channel_id: str) -> None:
        self.wait_seconds = wait_seconds
        self.channel_id = channel_id
        super().__init__(
            f"slow_mode: wait {wait_seconds:.1f}s on {channel_id}",
        )


@dataclass
class _SlowModeState:
    seconds_per_message: int = 0


class _SlowModeStore:
    def __init__(self, persist_path: Path) -> None:
        self.persist_path = persist_path
        self._caps: dict[str, _SlowModeState] = {}
        self._last_send: dict[tuple[str, str], float] = {}
        self._lock = threading.Lock()
        self._loaded = False

    # ── Persistence ──────────────────────────────────────────

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self.persist_path.is_file():
            return
        try:
            data = json.loads(self.persist_path.read_text("utf-8"))
            for cid, sec in (data or {}).items():
                self._caps[cid] = _SlowModeState(seconds_per_message=int(sec))
        except Exception as e:
            logger.warning("slow_mode_load_failed",
                           error=str(e), path=str(self.persist_path))

    def _save(self) -> None:
        try:
            self.persist_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                cid: s.seconds_per_message
                for cid, s in self._caps.items()
                if s.seconds_per_message > 0
            }
            tmp = self.persist_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2), "utf-8")
            tmp.replace(self.persist_path)
        except Exception as e:
            logger.warning("slow_mode_save_failed",
                           error=str(e), path=str(self.persist_path))

    # ── API ──────────────────────────────────────────────────

    def get(self, channel_id: str) -> int:
        with self._lock:
            self._load()
            s = self._caps.get(channel_id)
            return s.seconds_per_message if s else 0

    def set(self, channel_id: str, seconds: int) -> int:
        seconds = max(0, min(int(seconds), 21600))  # cap at 6 hours
        with self._lock:
            self._load()
            if seconds == 0:
                self._caps.pop(channel_id, None)
            else:
                self._caps[channel_id] = _SlowModeState(seconds)
            self._save()
        logger.info("slow_mode_changed",
                    channel_id=channel_id, seconds=seconds)
        return seconds

    def check_send_allowed(
        self,
        channel_id: str,
        sender_id: str,
        *,
        is_admin: bool = False,
    ) -> None:
        """Raises ``ChannelSlowModeError`` if the sender is too fast.
        Admins bypass. Records the send timestamp on success so the
        next call sees the new lower bound."""
        with self._lock:
            self._load()
            cap = self._caps.get(channel_id)
            if not cap or cap.seconds_per_message <= 0:
                return
            if is_admin:
                return
            now = time.monotonic()
            key = (channel_id, sender_id)
            last = self._last_send.get(key)
            if last is not None:
                elapsed = now - last
                if elapsed < cap.seconds_per_message:
                    raise ChannelSlowModeError(
                        wait_seconds=cap.seconds_per_message - elapsed,
                        channel_id=channel_id,
                    )
            self._last_send[key] = now


# ── Singleton ────────────────────────────────────────────────────


_store: Optional[_SlowModeStore] = None


def _get_store() -> _SlowModeStore:
    global _store
    if _store is None:
        env_path = os.environ.get("HELEN_SLOW_MODE_PATH")
        if env_path:
            path = Path(env_path)
        else:
            try:
                from app.core.config import get_settings
                path = (get_settings().PROJECT_ROOT
                        / "data" / "channel_slow_mode.json")
            except Exception:
                path = Path("data/channel_slow_mode.json")
        _store = _SlowModeStore(path)
    return _store


def get_slow_mode_seconds(channel_id: str) -> int:
    return _get_store().get(channel_id)


def set_slow_mode_seconds(channel_id: str, seconds: int) -> int:
    return _get_store().set(channel_id, seconds)


def check_send_allowed(
    channel_id: str, sender_id: str, *, is_admin: bool = False,
) -> None:
    _get_store().check_send_allowed(
        channel_id, sender_id, is_admin=is_admin,
    )


__all__ = [
    "ChannelSlowModeError",
    "get_slow_mode_seconds",
    "set_slow_mode_seconds",
    "check_send_allowed",
]
