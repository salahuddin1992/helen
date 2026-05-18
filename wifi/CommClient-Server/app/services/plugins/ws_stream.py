"""
Phase 7 / Module AH — Plugin Operations WebSocket
==================================================

Fan-out for install / upgrade / uninstall / sandbox-preview progress
events. Mirrors the architecture of
:class:`app.services.audit.ws_stream.AuditWebSocketManager`.

Wire format (JSON over text frames):

    {"type": "progress", "job_id": "...", "phase": "download",
     "pct": 42, "ts": "...", "detail": {...}}
    {"type": "log",      "job_id": "...", "level": "info", "msg": "..."}
    {"type": "error",    "job_id": "...", "phase": "verify",
     "error": "...", "fatal": true}
    {"type": "done",     "job_id": "...", "ok": true, "duration_ms": 1234}
    {"type": "ping",     "ts": "..."}
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Optional

from app.core.logging import get_logger

logger = get_logger(__name__)


class _Sub:
    __slots__ = ("ws", "queue", "alive", "filters")

    def __init__(self, ws: Any, filters: Optional[dict[str, Any]] = None) -> None:
        self.ws = ws
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=512)
        self.alive = True
        self.filters = filters or {}


class PluginsWebSocketManager:
    """Process-local pub/sub for plugin lifecycle events."""

    def __init__(self) -> None:
        self._subs: set[_Sub] = set()
        self._lock = asyncio.Lock()

    # ----- lifecycle ---------------------------------------------------

    async def connect(self, ws: Any, filters: Optional[dict[str, Any]] = None) -> _Sub:
        await ws.accept()
        sub = _Sub(ws, filters=filters)
        async with self._lock:
            self._subs.add(sub)
        logger.info("plugins_ws_connected", n=len(self._subs))
        return sub

    async def disconnect(self, sub: _Sub) -> None:
        sub.alive = False
        async with self._lock:
            self._subs.discard(sub)
        try:
            await sub.ws.close()
        except Exception:                                                # noqa: BLE001
            pass

    # ----- broadcast ---------------------------------------------------

    def _match(self, payload: dict[str, Any], filters: dict[str, Any]) -> bool:
        if not filters:
            return True
        if "job_id" in filters and payload.get("job_id") != filters["job_id"]:
            return False
        if "type" in filters and payload.get("type") != filters["type"]:
            return False
        if "slug" in filters and (payload.get("slug") != filters["slug"]):
            return False
        return True

    async def broadcast(self, payload: dict[str, Any]) -> None:
        payload.setdefault("ts", datetime.now(timezone.utc).isoformat())
        dead: list[_Sub] = []
        for sub in list(self._subs):
            if not sub.alive:
                dead.append(sub)
                continue
            if not self._match(payload, sub.filters):
                continue
            try:
                sub.queue.put_nowait(payload)
            except asyncio.QueueFull:
                sub.alive = False
                dead.append(sub)
                logger.warning("plugins_ws_queue_full")
        for sub in dead:
            await self.disconnect(sub)

    # ----- typed helpers ----------------------------------------------

    async def emit_progress(
        self, *, job_id: str, phase: str, pct: int,
        slug: Optional[str] = None, detail: Optional[dict[str, Any]] = None,
    ) -> None:
        await self.broadcast({
            "type": "progress", "job_id": job_id, "slug": slug,
            "phase": phase, "pct": max(0, min(100, int(pct))),
            "detail": detail or {},
        })

    async def emit_log(
        self, *, job_id: str, level: str, msg: str,
        slug: Optional[str] = None,
    ) -> None:
        await self.broadcast({
            "type": "log", "job_id": job_id, "slug": slug,
            "level": level, "msg": msg,
        })

    async def emit_error(
        self, *, job_id: str, phase: str, error: str,
        slug: Optional[str] = None, fatal: bool = False,
    ) -> None:
        await self.broadcast({
            "type": "error", "job_id": job_id, "slug": slug,
            "phase": phase, "error": error, "fatal": fatal,
        })

    async def emit_done(
        self, *, job_id: str, ok: bool, duration_ms: int,
        slug: Optional[str] = None, result: Optional[dict[str, Any]] = None,
    ) -> None:
        await self.broadcast({
            "type": "done", "job_id": job_id, "slug": slug,
            "ok": ok, "duration_ms": duration_ms,
            "result": result or {},
        })

    # ----- pump --------------------------------------------------------

    async def pump(self, sub: _Sub) -> None:
        try:
            while sub.alive:
                try:
                    msg = await asyncio.wait_for(sub.queue.get(), timeout=25)
                    await sub.ws.send_text(json.dumps(msg, default=str))
                except asyncio.TimeoutError:
                    await sub.ws.send_text(json.dumps({
                        "type": "ping",
                        "ts": datetime.now(timezone.utc).isoformat(),
                    }))
                except Exception:                                        # noqa: BLE001
                    break
        finally:
            await self.disconnect(sub)

    def stats(self) -> dict[str, Any]:
        return {"subscribers": len(self._subs)}


_manager: Optional[PluginsWebSocketManager] = None


def get_plugins_ws_manager() -> PluginsWebSocketManager:
    global _manager
    if _manager is None:
        _manager = PluginsWebSocketManager()
    return _manager


__all__ = ["PluginsWebSocketManager", "get_plugins_ws_manager"]
