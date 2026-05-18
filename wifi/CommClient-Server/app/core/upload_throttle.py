"""
HTTP upload throttle — per-user sliding-window file-upload rate limiter.

Distinct from the Socket.IO event-rate-limiter (app.socket.rate_limiter).
That one gates signalling and chat messages; this one enforces ceilings
on the expensive REST upload path:

  * maximum number of file uploads per user in a rolling window
  * maximum total bytes uploaded per user in the same window
  * maximum concurrent in-flight uploads per user

Returns a ``ThrottleError`` on breach so the caller can translate into a
``429 Too Many Requests``. Uses an asyncio lock so concurrent uploads from
the same user can't race on the counters.

This module is process-local on purpose — for a single-server LAN
deployment it's exactly what we want. A future multi-node cluster would
swap the dict for Redis without changing call sites.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Deque

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)
settings = get_settings()


class ThrottleError(Exception):
    """Raised when an upload would exceed a throttle policy."""

    def __init__(self, reason: str, retry_after_seconds: float | None = None):
        super().__init__(reason)
        self.reason = reason
        self.retry_after_seconds = retry_after_seconds


@dataclass
class _UserState:
    # (timestamp, bytes) — oldest first
    window: Deque[tuple[float, int]] = field(default_factory=deque)
    in_flight: int = 0


class UploadThrottle:
    """
    Sliding-window upload throttle per user.

    Counters are keyed by user_id. Both the event-count and byte-count
    checks run against the same window.

    Usage
    -----
    Always pair ``acquire`` with ``release`` so in-flight counts stay
    accurate even on error paths. Prefer the ``slot`` async context
    manager when possible.
    """

    def __init__(
        self,
        *,
        max_files: int | None = None,
        max_bytes: int | None = None,
        window_seconds: int | None = None,
        max_concurrent: int | None = None,
    ) -> None:
        self.max_files = max_files or settings.UPLOAD_RATE_MAX_FILES
        self.max_bytes = max_bytes or settings.UPLOAD_RATE_MAX_BYTES
        self.window_seconds = window_seconds or settings.UPLOAD_RATE_WINDOW_SEC
        self.max_concurrent = max_concurrent or settings.UPLOAD_MAX_CONCURRENT
        self._states: dict[str, _UserState] = defaultdict(_UserState)
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    # ── Core API ───────────────────────────────────────────────

    def _prune(self, state: _UserState, now: float) -> None:
        cutoff = now - self.window_seconds
        w = state.window
        while w and w[0][0] < cutoff:
            w.popleft()

    def _counts(self, state: _UserState) -> tuple[int, int]:
        events = len(state.window)
        total_bytes = sum(b for _, b in state.window)
        return events, total_bytes

    async def acquire(
        self,
        user_id: str,
        size_bytes: int,
    ) -> None:
        """
        Reserve a slot for an upload. Raises ``ThrottleError`` on breach.

        ``size_bytes`` must be the full anticipated size of the upload
        (for resumable uploads, that's the total declared size — not the
        per-chunk size, which would let clients slip past the byte cap).
        """
        if size_bytes < 0:
            raise ThrottleError("invalid negative upload size")

        # Pre-check: reject a single upload that alone is bigger than the
        # per-window byte cap. This is almost always a misconfiguration
        # on the server side — surface early rather than silently
        # starving the client. Skip when max_bytes <= 0 (unlimited).
        if self.max_bytes > 0 and size_bytes > self.max_bytes:
            raise ThrottleError(
                f"upload size {size_bytes} exceeds per-window byte cap {self.max_bytes}",
                retry_after_seconds=float(self.window_seconds),
            )

        async with self._locks[user_id]:
            now = time.monotonic()
            state = self._states[user_id]
            self._prune(state, now)

            events, total_bytes = self._counts(state)

            if self.max_concurrent > 0 and state.in_flight >= self.max_concurrent:
                logger.warning(
                    "upload_throttle_concurrent",
                    user_id=user_id,
                    in_flight=state.in_flight,
                    limit=self.max_concurrent,
                )
                raise ThrottleError(
                    f"max concurrent uploads ({self.max_concurrent}) reached",
                    retry_after_seconds=1.0,
                )

            if self.max_files > 0 and events >= self.max_files:
                retry = max(
                    0.0,
                    self.window_seconds - (now - state.window[0][0]),
                )
                logger.warning(
                    "upload_throttle_count",
                    user_id=user_id,
                    events=events,
                    limit=self.max_files,
                    retry_after=retry,
                )
                raise ThrottleError(
                    f"upload count limit ({self.max_files} / {self.window_seconds}s) reached",
                    retry_after_seconds=retry,
                )

            if self.max_bytes > 0 and total_bytes + size_bytes > self.max_bytes:
                retry = max(
                    0.0,
                    self.window_seconds - (now - state.window[0][0]),
                )
                logger.warning(
                    "upload_throttle_bytes",
                    user_id=user_id,
                    window_bytes=total_bytes,
                    candidate=size_bytes,
                    limit=self.max_bytes,
                    retry_after=retry,
                )
                raise ThrottleError(
                    f"byte quota ({self.max_bytes} / {self.window_seconds}s) would be exceeded",
                    retry_after_seconds=retry,
                )

            # Reserve the slot — both the window entry and the in-flight counter.
            state.window.append((now, size_bytes))
            state.in_flight += 1

    async def release_inflight(self, user_id: str) -> None:
        """
        Decrement only the in-flight counter without touching the window.

        Use this when a user's "concurrency slot" should be freed as
        soon as the request returns (e.g. a resumable-upload ``init``
        call), but the byte/count reservation still needs to stay in
        the rolling window.
        """
        async with self._locks[user_id]:
            state = self._states.get(user_id)
            if not state:
                return
            if state.in_flight > 0:
                state.in_flight -= 1

    async def release(self, user_id: str, *, success: bool = True) -> None:
        """
        Release an in-flight slot. If the upload failed before producing
        any bytes, the caller may pass ``success=False`` to remove the
        reservation entry so it doesn't count against the byte cap — we
        still drop the most recent entry for that user.
        """
        async with self._locks[user_id]:
            state = self._states.get(user_id)
            if not state:
                return
            if state.in_flight > 0:
                state.in_flight -= 1
            if not success and state.window:
                # Pop the most-recent reservation we just made in acquire().
                state.window.pop()
            # Opportunistically garbage-collect empty users.
            if state.in_flight == 0 and not state.window:
                self._states.pop(user_id, None)
                self._locks.pop(user_id, None)

    def stats(self, user_id: str) -> dict[str, int | float]:
        """Return a snapshot of the user's current throttle state."""
        state = self._states.get(user_id)
        if not state:
            return {
                "events": 0,
                "window_bytes": 0,
                "in_flight": 0,
                "max_files": self.max_files,
                "max_bytes": self.max_bytes,
                "window_seconds": self.window_seconds,
                "max_concurrent": self.max_concurrent,
            }
        now = time.monotonic()
        self._prune(state, now)
        events, total = self._counts(state)
        return {
            "events": events,
            "window_bytes": total,
            "in_flight": state.in_flight,
            "max_files": self.max_files,
            "max_bytes": self.max_bytes,
            "window_seconds": self.window_seconds,
            "max_concurrent": self.max_concurrent,
        }


# Singleton for the REST upload path.
upload_throttle = UploadThrottle()


class _UploadSlot:
    """Async context manager wrapper around ``acquire`` / ``release``."""

    def __init__(self, throttle: UploadThrottle, user_id: str, size_bytes: int):
        self._throttle = throttle
        self._user_id = user_id
        self._size_bytes = size_bytes
        self._acquired = False
        self._success = False

    async def __aenter__(self) -> "_UploadSlot":
        await self._throttle.acquire(self._user_id, self._size_bytes)
        self._acquired = True
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._acquired:
            # If an exception escapes the block, roll back the window entry
            # so a failed upload doesn't "count" against the byte cap.
            success = self._success and exc_type is None
            await self._throttle.release(self._user_id, success=success)

    def mark_success(self) -> None:
        self._success = True


def upload_slot(user_id: str, size_bytes: int) -> _UploadSlot:
    """Convenience context manager for the global singleton throttle."""
    return _UploadSlot(upload_throttle, user_id, size_bytes)
