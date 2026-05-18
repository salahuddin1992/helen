"""
Edge ↔ origin sync stream.

Bidirectional WebSocket pipe between an edge node and origin server.

Edge → origin   : telemetry, presence touches, worker results.
Origin → edge   : cache invalidations, config updates, policy changes.

Priority queue: events are tagged ``critical`` / ``normal`` / ``best_effort``.
On bandwidth pressure, ``best_effort`` is dropped first.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional

from app.core.logging import get_logger

logger = get_logger(__name__)


PRIORITY_CRITICAL = 0
PRIORITY_NORMAL   = 1
PRIORITY_BEST     = 2

MAX_PENDING = 5000


@dataclass(order=True)
class _Item:
    priority: int
    seq: int
    kind: str = field(compare=False)
    payload: dict[str, Any] = field(compare=False, default_factory=dict)
    ts: float = field(compare=False, default_factory=time.monotonic)


class EdgeSyncChannel:
    """Single duplex pipe for one edge ↔ origin pair."""

    def __init__(self, *, node_id: str, send: Any = None) -> None:
        self.node_id = node_id
        self._queue: deque[_Item] = deque()
        self._seq = 0
        self._lock = asyncio.Lock()
        self._send = send  # callable: send(dict) -> awaitable
        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task[None]] = None
        self._dropped = 0
        self._inflight = 0
        self._sent = 0
        self._received = 0

    async def enqueue(
        self, kind: str, payload: dict[str, Any], *,
        priority: int = PRIORITY_NORMAL,
    ) -> None:
        async with self._lock:
            self._seq += 1
            item = _Item(priority=priority, seq=self._seq, kind=kind, payload=payload)
            self._queue.append(item)
            self._queue = deque(sorted(self._queue))  # priority-sorted
            if len(self._queue) > MAX_PENDING:
                # Drop best-effort first.
                for _ in range(len(self._queue) - MAX_PENDING):
                    for i, it in enumerate(self._queue):
                        if it.priority == PRIORITY_BEST:
                            del self._queue[i]
                            self._dropped += 1
                            break
                    else:
                        # nothing best-effort, drop the tail
                        self._queue.pop()
                        self._dropped += 1

    async def start_pump(self, send: Any) -> None:
        self._send = send
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._pump(), name=f"edge-sync-{self.node_id}")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _pump(self) -> None:
        while not self._stop.is_set():
            async with self._lock:
                if not self._queue:
                    item = None
                else:
                    item = self._queue.popleft()
            if item is None:
                await asyncio.sleep(0.1)
                continue
            try:
                self._inflight += 1
                if self._send is not None:
                    await self._send({
                        "kind":    item.kind,
                        "payload": item.payload,
                        "seq":     item.seq,
                        "ts":      item.ts,
                    })
                    self._sent += 1
            except Exception as exc:
                logger.warning("edge_sync_send_failed node=%s err=%s",
                               self.node_id, exc)
            finally:
                self._inflight -= 1

    async def receive(self, message: dict[str, Any]) -> None:
        """Handle an inbound event from the edge."""
        self._received += 1
        kind = message.get("kind") or ""
        payload = message.get("payload") or {}
        await self._dispatch(kind, payload)

    async def _dispatch(self, kind: str, payload: dict[str, Any]) -> None:
        if kind == "telemetry":
            await self._on_telemetry(payload)
        elif kind == "presence":
            await self._on_presence(payload)
        elif kind == "worker_result":
            await self._on_worker_result(payload)

    async def _on_telemetry(self, payload: dict[str, Any]) -> None:
        # Persist heartbeat / load metric.
        try:
            from sqlalchemy import update
            from app.db.session import async_session_factory
            from app.models.edge import EdgeNode
            from datetime import datetime, timezone
            async with async_session_factory() as db:
                await db.execute(
                    update(EdgeNode)
                    .where(EdgeNode.node_id == self.node_id)
                    .values(
                        current_load_percent=float(payload.get("load_percent") or 0.0),
                        last_heartbeat=datetime.now(timezone.utc),
                    )
                )
                await db.commit()
        except Exception as exc:
            logger.debug("edge_telemetry_persist_failed err=%s", exc)

    async def _on_presence(self, payload: dict[str, Any]) -> None:
        # Forward to local presence service if available.
        try:
            from app.services.presence_service import presence_service
            user_id = payload.get("user_id")
            status = payload.get("status") or "online"
            if user_id and status == "online":
                await presence_service.set_status(user_id, "online")
        except Exception:
            pass

    async def _on_worker_result(self, payload: dict[str, Any]) -> None:
        # Stash worker result; caller awaits via correlation_id.
        global _worker_results
        cid = payload.get("correlation_id")
        if cid:
            _worker_results[cid] = payload

    def stats(self) -> dict[str, Any]:
        return {
            "node_id":  self.node_id,
            "queued":   len(self._queue),
            "inflight": self._inflight,
            "sent":     self._sent,
            "received": self._received,
            "dropped":  self._dropped,
        }


# ── singleton + worker correlation ──────────────────────────


_channels: dict[str, EdgeSyncChannel] = {}
_worker_results: dict[str, dict[str, Any]] = {}


def get_or_create_channel(node_id: str) -> EdgeSyncChannel:
    ch = _channels.get(node_id)
    if ch is None:
        ch = EdgeSyncChannel(node_id=node_id)
        _channels[node_id] = ch
    return ch


def list_channels() -> dict[str, dict[str, Any]]:
    return {nid: ch.stats() for nid, ch in _channels.items()}


def worker_results() -> dict[str, dict[str, Any]]:
    return dict(_worker_results)
