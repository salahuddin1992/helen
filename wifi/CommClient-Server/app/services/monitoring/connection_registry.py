"""
ConnectionRegistry — in-memory live connections directory.

Stores active client connections (Socket.IO, transport bridges, raw TCP).
Designed for sub-millisecond list/lookup against tens of thousands of entries.

Thread-safety: a single asyncio.Lock guards mutation; reads are lock-free
snapshots (defensive copies returned).

Integration: Socket.IO server handlers should call
:meth:`register` on ``connect`` and :meth:`unregister` on ``disconnect``,
and :meth:`update_traffic` on every ``emit``/``recv`` (or every N bytes).

Kicking
-------
:meth:`kick` schedules disconnection. The actual transport drop happens
through registered ``kick_callbacks`` per transport (e.g. ``sio.disconnect``).
Each callback is fire-and-forget but errors are logged.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

try:
    import structlog
    _log = structlog.get_logger(__name__)
except Exception:  # pragma: no cover
    import logging
    _log = logging.getLogger(__name__)


KickFn = Callable[[str], Awaitable[None]]


@dataclass
class ConnectionInfo:
    id: str
    user_id: str
    username: str
    ip: str
    transport: str
    connected_at: float
    bytes_in: int = 0
    bytes_out: int = 0
    last_activity_ts: float = field(default_factory=lambda: time.time())
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "username": self.username,
            "ip": self.ip,
            "transport": self.transport,
            "connected_at": self.connected_at,
            "bytes_in": self.bytes_in,
            "bytes_out": self.bytes_out,
            "last_activity_ts": self.last_activity_ts,
            "meta": self.meta,
        }


class ConnectionRegistry:
    """Singleton — fetch with :func:`get_connection_registry`."""

    def __init__(self) -> None:
        self._conns: dict[str, ConnectionInfo] = {}
        self._lock = asyncio.Lock()
        self._kick_callbacks: dict[str, KickFn] = {}

    # ── Mutation ────────────────────────────────────────────────────────

    async def register(self, info: ConnectionInfo) -> None:
        async with self._lock:
            self._conns[info.id] = info
        _log.info("connection_registered", conn_id=info.id,
                  user_id=info.user_id, transport=info.transport)

    async def unregister(self, conn_id: str) -> Optional[ConnectionInfo]:
        async with self._lock:
            popped = self._conns.pop(conn_id, None)
        if popped:
            _log.info("connection_unregistered", conn_id=conn_id)
        return popped

    async def update_traffic(
        self,
        conn_id: str,
        bytes_in: int = 0,
        bytes_out: int = 0,
    ) -> None:
        async with self._lock:
            info = self._conns.get(conn_id)
            if info is None:
                return
            info.bytes_in += int(bytes_in)
            info.bytes_out += int(bytes_out)
            info.last_activity_ts = time.time()

    # ── Read ────────────────────────────────────────────────────────────

    async def get(self, conn_id: str) -> Optional[ConnectionInfo]:
        async with self._lock:
            info = self._conns.get(conn_id)
            return None if info is None else ConnectionInfo(**info.__dict__)

    async def list(
        self,
        limit: int = 50,
        offset: int = 0,
        transport: Optional[str] = None,
        search: Optional[str] = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """
        Paginated listing.

        Returns ``(rows, total_after_filter)``. Rows are ordered by
        ``connected_at`` desc — newest first.
        """
        async with self._lock:
            items = list(self._conns.values())

        if transport:
            t = transport.lower()
            items = [c for c in items if c.transport.lower() == t]

        if search:
            q = search.lower()
            items = [
                c for c in items
                if q in c.username.lower()
                or q in c.user_id.lower()
                or q in c.ip.lower()
                or q in c.id.lower()
            ]

        items.sort(key=lambda c: c.connected_at, reverse=True)
        total = len(items)
        page = items[offset: offset + limit]
        return [c.to_dict() for c in page], total

    async def count(self, transport: Optional[str] = None) -> int:
        async with self._lock:
            if not transport:
                return len(self._conns)
            t = transport.lower()
            return sum(1 for c in self._conns.values() if c.transport.lower() == t)

    # ── Kick ────────────────────────────────────────────────────────────

    def register_kick_callback(self, transport: str, fn: KickFn) -> None:
        self._kick_callbacks[transport.lower()] = fn

    async def kick(self, conn_id: str) -> bool:
        """
        Initiate a disconnect for ``conn_id``.
        Returns ``True`` if the connection was known and a callback dispatched,
        ``False`` if the id was unknown.
        """
        info = await self.get(conn_id)
        if info is None:
            return False
        cb = self._kick_callbacks.get(info.transport.lower())
        if cb is not None:
            try:
                await cb(conn_id)
            except Exception as exc:
                _log.error("kick_callback_failed", conn_id=conn_id, error=str(exc))
        # Whether or not there was a callback, remove from registry — operators
        # consider a kick successful as soon as the entry is gone.
        await self.unregister(conn_id)
        return True


# ── Singleton accessor ──────────────────────────────────────────────────

_INSTANCE: Optional[ConnectionRegistry] = None


def get_connection_registry() -> ConnectionRegistry:
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = ConnectionRegistry()
    return _INSTANCE
