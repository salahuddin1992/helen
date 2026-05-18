"""Persistent retry queue — survives process restart.

Stored as JSON-lines at ``data/resilience_retry_queue.jsonl`` so a
crash doesn't lose pending work. The queue holds:

    {
      "task_kind":  str,
      "payload":    dict,
      "attempt":    int,
      "next_at":    float (unix),
      "enqueued_at": float,
      "deadline":   float,
    }

A background dispatcher pops ready entries (next_at <= now) and
hands them off to caller-supplied handlers.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Awaitable, Callable, Optional

from app.core.logging import get_logger
from app.resilience.failure_classifier import FailureKind
from app.resilience.resilience_config import get_config
from app.resilience.resilience_events import emit
from app.resilience.resilience_exceptions import RetryExhaustedError
from app.resilience.retry_policy import compute_delay, should_retry

logger = get_logger(__name__)


_DATA_DIR = Path(os.environ.get("COMMCLIENT_DATA_DIR",
                 str(Path(__file__).resolve().parents[2] / "data")))
_QUEUE_FILE = _DATA_DIR / "resilience_retry_queue.jsonl"


# Handler signature: (payload) -> awaitable[bool]  (True = success)
HandlerFn = Callable[[dict], Awaitable[bool]]


class RetryQueue:
    _singleton: "RetryQueue | None" = None

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._items: deque = deque()
        self._handlers: dict[str, HandlerFn] = {}
        self._loop_task: Optional[asyncio.Task] = None
        self._running = False
        self._restored = False

    @classmethod
    def instance(cls) -> "RetryQueue":
        if cls._singleton is None:
            cls._singleton = RetryQueue()
        return cls._singleton

    # ── Public API ─────────────────────────────────────────

    def register_handler(self, task_kind: str, handler: HandlerFn) -> None:
        with self._lock:
            self._handlers[task_kind] = handler

    def enqueue(self, task_kind: str, payload: dict,
                *, attempt: int = 0,
                failure_kind: FailureKind = FailureKind.TRANSIENT) -> str:
        cfg = get_config()
        if not should_retry(attempt, failure_kind):
            raise RetryExhaustedError(
                f"task_kind={task_kind} attempt={attempt} not retryable"
            )
        with self._lock:
            if len(self._items) >= cfg.retry_queue_max:
                # Drop oldest to make room — better than blocking the
                # producer; the dropped entry will time out.
                self._items.popleft()
            now = time.time()
            entry = {
                "id":          uuid.uuid4().hex,
                "task_kind":   task_kind,
                "payload":     payload,
                "attempt":     int(attempt),
                "next_at":     now + compute_delay(attempt, failure_kind=failure_kind),
                "enqueued_at": now,
                "deadline":    now + cfg.retry_queue_ttl_sec,
            }
            self._items.append(entry)
            self._persist_locked()
        emit("retry.enqueued", {"task_kind": task_kind, "attempt": attempt})
        return entry["id"]

    # ── Persistence ───────────────────────────────────────

    def _persist_locked(self) -> None:
        try:
            _DATA_DIR.mkdir(parents=True, exist_ok=True)
            tmp = _QUEUE_FILE.with_suffix(".tmp")
            with tmp.open("w", encoding="utf-8") as f:
                for entry in self._items:
                    f.write(json.dumps(entry) + "\n")
            tmp.replace(_QUEUE_FILE)
        except Exception as e:
            logger.warning("retry_queue_persist_failed", error=str(e))

    def restore(self) -> int:
        if self._restored:
            return 0
        self._restored = True
        if not _QUEUE_FILE.is_file():
            return 0
        with self._lock:
            try:
                with _QUEUE_FILE.open("r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            self._items.append(json.loads(line))
                        except Exception:
                            continue
            except Exception as e:
                logger.warning("retry_queue_restore_failed", error=str(e))
        return len(self._items)

    # ── Dispatcher loop ────────────────────────────────────

    async def _dispatch(self, entry: dict) -> bool:
        kind = entry.get("task_kind") or ""
        with self._lock:
            handler = self._handlers.get(kind)
        if handler is None:
            logger.warning("retry_queue_no_handler", task_kind=kind)
            return False
        try:
            return bool(await handler(entry.get("payload") or {}))
        except Exception as e:
            logger.warning("retry_handler_raised",
                           task_kind=kind, error=str(e)[:80])
            return False

    async def _run_loop(self) -> None:
        self._running = True
        self.restore()
        logger.info("retry_queue_started",
                    pending=len(self._items))
        try:
            while self._running:
                now = time.time()
                ready: list[dict] = []
                with self._lock:
                    remaining = deque()
                    for entry in self._items:
                        if entry.get("deadline", 0) < now:
                            emit("retry.expired", {
                                "task_kind": entry.get("task_kind"),
                                "id": entry.get("id"),
                            })
                            continue
                        if entry.get("next_at", 0) <= now:
                            ready.append(entry)
                        else:
                            remaining.append(entry)
                    self._items = remaining
                    self._persist_locked()

                # Process ready entries one at a time so a slow
                # handler doesn't starve the loop.
                for entry in ready:
                    ok = await self._dispatch(entry)
                    if ok:
                        emit("retry.ok", {"task_kind": entry.get("task_kind")})
                        continue
                    # Re-enqueue with incremented attempt.
                    next_attempt = int(entry.get("attempt", 0)) + 1
                    try:
                        self.enqueue(
                            entry["task_kind"], entry["payload"],
                            attempt=next_attempt,
                        )
                    except RetryExhaustedError:
                        emit("retry.exhausted", {
                            "task_kind": entry.get("task_kind"),
                            "id": entry.get("id"),
                            "attempts": next_attempt,
                        })
                await asyncio.sleep(1.0)
        finally:
            logger.info("retry_queue_stopped")

    def start(self) -> None:
        if self._loop_task is not None and not self._loop_task.done():
            return
        try:
            loop = asyncio.get_event_loop()
            self._loop_task = loop.create_task(
                self._run_loop(), name="resilience-retry-queue",
            )
        except RuntimeError:
            logger.warning("retry_queue_no_event_loop_yet")

    def stop(self) -> None:
        self._running = False
        if self._loop_task is not None:
            self._loop_task.cancel()
            self._loop_task = None

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "pending":   len(self._items),
                "handlers":  sorted(self._handlers.keys()),
                "queue_file": str(_QUEUE_FILE),
            }


def get_retry_queue() -> RetryQueue:
    return RetryQueue.instance()
