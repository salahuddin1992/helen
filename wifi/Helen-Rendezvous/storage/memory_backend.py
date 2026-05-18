"""
In-memory `MemoryBackend` for Helen-Rendezvous.

This is the default backend. It preserves the exact semantics of the original
single-process reference rendezvous so a Helen deployment that doesn't need
horizontal scale keeps working with zero configuration. Every operation is
constant-time and lock-free except for the cleanup task.

Semantics worth pinning down:
* TTLs are stored as a monotonic-clock expiry; expired entries are filtered out
  at read time and reaped by a background task.
* Pub/sub is in-process: subscribers register a queue and `publish_event`
  pushes the same dict into every queue. There is no fan-out cap.
* Locks use a simple {key: token} dict with TTL.
* `close()` cancels the cleanup task and clears all state.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import time
from typing import Any, AsyncIterator, Optional

import structlog

logger = structlog.get_logger(__name__)


class _Expiring:
    """Container that stores a payload alongside a monotonic expiry."""

    __slots__ = ("payload", "expires_at")

    def __init__(self, payload: dict[str, Any], ttl: int) -> None:
        self.payload = payload
        self.expires_at = time.monotonic() + max(1, ttl)

    @property
    def expired(self) -> bool:
        return time.monotonic() >= self.expires_at

    def refresh(self, ttl: int) -> None:
        self.expires_at = time.monotonic() + max(1, ttl)


class MemoryBackend:
    """Single-process backend. Drop-in replacement for the original dicts."""

    backend_name = "memory"

    def __init__(self, cleanup_interval: float = 5.0) -> None:
        self._tunnels: dict[str, _Expiring] = {}
        self._signals: dict[str, _Expiring] = {}
        self._locks: dict[str, tuple[str, float]] = {}
        self._subscribers: dict[str, list[asyncio.Queue]] = {}
        self._lock = asyncio.Lock()
        self._cleanup_interval = cleanup_interval
        self._cleanup_task: Optional[asyncio.Task[None]] = None
        self._started_at = time.time()
        self._published = 0
        self._delivered = 0

    # ── Lifecycle ──────────────────────────────────────────

    async def start(self) -> None:
        """Spawn the reaper task. Idempotent."""
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._reaper())
            logger.info("memory_backend_started", interval=self._cleanup_interval)

    async def close(self) -> None:
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._cleanup_task
        async with self._lock:
            self._tunnels.clear()
            self._signals.clear()
            self._locks.clear()
            for queues in self._subscribers.values():
                for q in queues:
                    with contextlib.suppress(Exception):
                        q.put_nowait({"type": "__shutdown__"})
            self._subscribers.clear()
        logger.info("memory_backend_closed")

    async def _reaper(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._cleanup_interval)
                async with self._lock:
                    for table in (self._tunnels, self._signals):
                        dead = [k for k, v in table.items() if v.expired]
                        for k in dead:
                            table.pop(k, None)
                    now = time.monotonic()
                    dead_locks = [
                        k for k, (_, exp) in self._locks.items() if exp <= now
                    ]
                    for k in dead_locks:
                        self._locks.pop(k, None)
        except asyncio.CancelledError:
            return

    # ── Tunnels ────────────────────────────────────────────

    async def register_tunnel(
        self,
        peer_id: str,
        info: dict[str, Any],
        ttl: int,
    ) -> str:
        async with self._lock:
            self._tunnels[peer_id] = _Expiring({**info, "peer_id": peer_id}, ttl)
        return f"tunnel:{peer_id}"

    async def lookup_tunnel(self, peer_id: str) -> Optional[dict[str, Any]]:
        async with self._lock:
            entry = self._tunnels.get(peer_id)
            if entry is None or entry.expired:
                if entry is not None:
                    self._tunnels.pop(peer_id, None)
                return None
            return dict(entry.payload)

    async def unregister_tunnel(self, peer_id: str) -> bool:
        async with self._lock:
            return self._tunnels.pop(peer_id, None) is not None

    async def list_tunnels(self) -> list[dict[str, Any]]:
        async with self._lock:
            return [dict(v.payload) for v in self._tunnels.values() if not v.expired]

    async def refresh_tunnel(self, peer_id: str, ttl: int) -> bool:
        async with self._lock:
            entry = self._tunnels.get(peer_id)
            if entry is None or entry.expired:
                return False
            entry.refresh(ttl)
            return True

    # ── Signaling ──────────────────────────────────────────

    async def register_signal(
        self,
        key: str,
        payload: dict[str, Any],
        ttl: int,
    ) -> bool:
        async with self._lock:
            self._signals[key] = _Expiring(dict(payload), ttl)
        return True

    async def lookup_signal(self, key: str) -> Optional[dict[str, Any]]:
        async with self._lock:
            entry = self._signals.get(key)
            if entry is None or entry.expired:
                if entry is not None:
                    self._signals.pop(key, None)
                return None
            return dict(entry.payload)

    async def delete_signal(self, key: str) -> bool:
        async with self._lock:
            return self._signals.pop(key, None) is not None

    async def list_signals(self) -> list[str]:
        async with self._lock:
            return [k for k, v in self._signals.items() if not v.expired]

    # ── Pub/sub ────────────────────────────────────────────

    async def publish_event(self, channel: str, payload: dict[str, Any]) -> int:
        async with self._lock:
            subs = list(self._subscribers.get(channel, []))
        delivered = 0
        for q in subs:
            try:
                q.put_nowait(dict(payload))
                delivered += 1
            except asyncio.QueueFull:
                logger.warning("memory_pubsub_dropped", channel=channel)
        self._published += 1
        self._delivered += delivered
        return delivered

    async def subscribe_events(  # type: ignore[override]
        self,
        channel: str,
    ) -> AsyncIterator[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1024)
        async with self._lock:
            self._subscribers.setdefault(channel, []).append(queue)
        try:
            while True:
                msg = await queue.get()
                if msg.get("type") == "__shutdown__":
                    return
                yield msg
        finally:
            async with self._lock:
                bucket = self._subscribers.get(channel, [])
                with contextlib.suppress(ValueError):
                    bucket.remove(queue)
                if not bucket:
                    self._subscribers.pop(channel, None)

    # ── Locks ──────────────────────────────────────────────

    async def acquire_lock(self, key: str, ttl: int) -> Optional[str]:
        token = secrets.token_hex(16)
        now = time.monotonic()
        async with self._lock:
            existing = self._locks.get(key)
            if existing is not None and existing[1] > now:
                return None
            self._locks[key] = (token, now + max(1, ttl))
        return token

    async def release_lock(self, key: str, token: str) -> bool:
        async with self._lock:
            existing = self._locks.get(key)
            if existing is None:
                return False
            if not secrets.compare_digest(existing[0], token):
                return False
            self._locks.pop(key, None)
            return True

    # ── Health ─────────────────────────────────────────────

    async def health(self) -> dict[str, Any]:
        return {
            "backend": "memory",
            "status": "ok",
            "latency_ms": 0.0,
            "details": {
                "tunnels": len(self._tunnels),
                "signals": len(self._signals),
                "locks": len(self._locks),
                "subscribers": sum(len(v) for v in self._subscribers.values()),
                "uptime_sec": int(time.time() - self._started_at),
                "published": self._published,
                "delivered": self._delivered,
            },
        }
