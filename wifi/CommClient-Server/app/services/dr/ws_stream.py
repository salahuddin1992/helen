"""
DR v2 WebSocket fan-out.

Connects every WS client to a single broadcast hub.  The hub subscribes
to :class:`JobRegistry` events and pushes them to every connected
socket.  Additional event names (``backup.completed``, ``destination.
changed``, ``integrity.alert``, ``drill.completed``) are emitted from
the respective services via :func:`broadcast`.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Set

from fastapi import WebSocket, WebSocketDisconnect

from app.core.logging import get_logger
from app.services.dr.job_registry import dr_job_registry


logger = get_logger(__name__)


class DRWebSocketManager:
    def __init__(self) -> None:
        self._clients: Set[WebSocket] = set()
        self._lock = asyncio.Lock()
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._pump_jobs(), name="dr_v2_ws_pump")
        logger.info("dr_v2_ws_pump_started")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=3.0)
            except asyncio.TimeoutError:
                self._task.cancel()
        self._task = None

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._clients.add(ws)
        await ws.send_json({"event": "ws.hello",
                            "ts": datetime.now(timezone.utc).isoformat(),
                            "clients": len(self._clients)})
        logger.info("dr_v2_ws_connected", clients=len(self._clients))

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)
        logger.info("dr_v2_ws_disconnected", clients=len(self._clients))

    async def broadcast(self, event: str, data: Dict[str, Any]) -> None:
        payload = {"event": event, "data": data,
                   "ts": datetime.now(timezone.utc).isoformat()}
        dead: list[WebSocket] = []
        async with self._lock:
            clients = list(self._clients)
        for ws in clients:
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for d in dead:
                    self._clients.discard(d)

    async def serve(self, ws: WebSocket) -> None:
        """Block on a WS connection until the client disconnects."""
        await self.connect(ws)
        try:
            while True:
                raw = await ws.receive_text()
                if raw.strip().lower() in ("ping", "\"ping\""):
                    await ws.send_json(
                        {"event": "pong",
                         "ts": datetime.now(timezone.utc).isoformat()},
                    )
        except WebSocketDisconnect:
            pass
        except Exception:
            logger.exception("dr_v2_ws_recv_error")
        finally:
            await self.disconnect(ws)

    async def _pump_jobs(self) -> None:
        queue = dr_job_registry.subscribe()
        try:
            while not self._stop.is_set():
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=2.0)
                except asyncio.TimeoutError:
                    continue
                ev = msg.get("event", "job.update")
                data = msg.get("data") or {}
                await self.broadcast(ev, data)
        finally:
            dr_job_registry.unsubscribe(queue)


dr_ws_manager = DRWebSocketManager()
