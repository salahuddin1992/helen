"""Push-notification offline batcher + retry queue.

When a recipient is offline (no Socket.IO + no fresh device token),
push notifications pile up. Without batching:
  * Each message becomes its own push → noise bomb when user comes back.
  * FCM/APNs rate-limit our app for repeated payloads.

This batcher collects per-user pushes for a configurable window
(default 30s) and either:
  * Flushes one combined "5 new messages" notification, or
  * Discards if the user came back online during the window.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional


def _i(env: str, default: int) -> int:
    try:
        return int(os.environ.get(env) or default)
    except (TypeError, ValueError):
        return default


def _f(env: str, default: float) -> float:
    try:
        return float(os.environ.get(env) or default)
    except (TypeError, ValueError):
        return default


BATCH_WINDOW_SEC = _f("HELEN_PUSH_BATCH_SEC", 30.0)
MAX_BATCH_SIZE   = _i("HELEN_PUSH_BATCH_MAX", 50)


@dataclass
class _UserBatch:
    user_id:     str
    payloads:    list[dict] = field(default_factory=list)
    first_at:    float = 0.0


# Sender signature: (user_id, combined_payload) → awaitable[bool]
SenderFn = Callable[[str, dict], Awaitable[bool]]


class PushOfflineBatcher:
    _singleton: "PushOfflineBatcher | None" = None

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._batches: dict[str, _UserBatch] = {}
        self._sender: Optional[SenderFn] = None
        self._loop_task: Optional[asyncio.Task] = None
        self._running = False
        self._stats = {
            "queued": 0, "flushed": 0, "discarded": 0,
            "dropped_overflow": 0, "send_failures": 0,
        }

    @classmethod
    def instance(cls) -> "PushOfflineBatcher":
        if cls._singleton is None:
            cls._singleton = PushOfflineBatcher()
        return cls._singleton

    # ── Configuration ──────────────────────────────────────

    def set_sender(self, sender: SenderFn) -> None:
        with self._lock:
            self._sender = sender

    # ── Mutators ───────────────────────────────────────────

    def queue(self, user_id: str, payload: dict) -> None:
        if not user_id:
            return
        with self._lock:
            batch = self._batches.get(user_id)
            if batch is None:
                batch = _UserBatch(user_id=user_id, first_at=time.time())
                self._batches[user_id] = batch
            if len(batch.payloads) < MAX_BATCH_SIZE:
                batch.payloads.append(payload)
                self._stats["queued"] += 1
            else:
                # Hit MAX_BATCH_SIZE — keep the newest by replacing the
                # second-to-last (we always preserve `last` for the
                # combined preview). Track the drop so monitoring can
                # alert if a recipient is being overwhelmed.
                if len(batch.payloads) >= 2:
                    batch.payloads[-1] = payload
                self._stats["dropped_overflow"] += 1

    def discard_for(self, user_id: str) -> int:
        """Called when user comes online before flush."""
        with self._lock:
            b = self._batches.pop(user_id, None)
            if b is None:
                return 0
            n = len(b.payloads)
            self._stats["discarded"] += n
            return n

    # ── Flusher ────────────────────────────────────────────

    async def _flush_due(self) -> None:
        now = time.time()
        ready: list[_UserBatch] = []
        with self._lock:
            for uid, b in list(self._batches.items()):
                if now - b.first_at >= BATCH_WINDOW_SEC:
                    ready.append(b)
                    self._batches.pop(uid, None)
            sender = self._sender

        if not ready or sender is None:
            return

        for b in ready:
            combined = {
                "kind":  "batch",
                "count": len(b.payloads),
                "first": b.payloads[0] if b.payloads else None,
                "last":  b.payloads[-1] if b.payloads else None,
            }
            ok = False
            for attempt in range(2):
                try:
                    ok = await sender(b.user_id, combined)
                    if ok:
                        break
                except Exception:
                    ok = False
                if attempt == 0:
                    await asyncio.sleep(1.0)
            with self._lock:
                if ok:
                    self._stats["flushed"] += len(b.payloads)
                else:
                    self._stats["send_failures"] += 1

    async def _run_loop(self) -> None:
        self._running = True
        try:
            while self._running:
                try:
                    await self._flush_due()
                except Exception:
                    pass
                await asyncio.sleep(min(5.0, BATCH_WINDOW_SEC / 2))
        finally:
            pass

    def start(self) -> None:
        if self._loop_task is not None and not self._loop_task.done():
            return
        try:
            loop = asyncio.get_event_loop()
            self._loop_task = loop.create_task(
                self._run_loop(), name="push-offline-batcher",
            )
        except RuntimeError:
            pass

    def stop(self) -> None:
        self._running = False
        if self._loop_task is not None:
            self._loop_task.cancel()
            self._loop_task = None

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "pending_users":  len(self._batches),
                "stats":          dict(self._stats),
                "window_sec":     BATCH_WINDOW_SEC,
                "max_batch_size": MAX_BATCH_SIZE,
            }


def get_push_batcher() -> PushOfflineBatcher:
    return PushOfflineBatcher.instance()
