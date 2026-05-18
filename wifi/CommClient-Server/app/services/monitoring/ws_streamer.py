"""
MetricsWebSocketManager — broadcaster for the ``/api/admin/ws/metrics`` channel.

- Validates admin tokens on connect (Authorization bearer header or ?token=)
- Pushes a metrics frame every 1s
- Pushes any new alerts produced by ``MetricsCollector`` as they appear
- Cleanly drops dead clients without blocking the broadcast loop
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Optional

from fastapi import WebSocket
from starlette.websockets import WebSocketState

try:
    import structlog
    _log = structlog.get_logger(__name__)
except Exception:  # pragma: no cover
    import logging
    _log = logging.getLogger(__name__)

from app.services.monitoring.metrics_collector import get_metrics_collector


# ── Auth shim ───────────────────────────────────────────────────────────


def verify_admin_token(token: str) -> Optional[str]:
    """
    Validate a token and ensure it carries at least the admin role.

    Returns the ``user_id`` on success, or ``None`` on any failure
    (expired/invalid/insufficient role).
    """
    if not token:
        return None
    try:
        from app.core.security import decode_token
        payload = decode_token(token)
    except Exception:
        return None
    if payload.get("type") != "access":
        return None
    role = payload.get("role", "user")
    # admin or higher only
    if role not in ("admin", "superadmin", "root"):
        return None
    user_id = payload.get("sub")
    if not user_id:
        return None
    return str(user_id)


# ── Manager ─────────────────────────────────────────────────────────────


class MetricsWebSocketManager:
    """Singleton — fetch via :func:`get_ws_manager`."""

    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()
        self._task: Optional[asyncio.Task[None]] = None
        self._running = False
        self._last_alert_ts: float = 0.0

    # ── Lifecycle ───────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._broadcast_loop(), name="metrics_ws_broadcast")
        _log.info("metrics_ws_started")

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        async with self._lock:
            for ws in list(self._clients):
                try:
                    await ws.close()
                except Exception:
                    pass
            self._clients.clear()

    # ── Connection management ───────────────────────────────────────────

    async def connect(self, websocket: WebSocket, token: str) -> bool:
        """
        Accept the websocket if the token is valid.

        Returns ``True`` on success, ``False`` otherwise — caller is
        responsible for closing the socket if ``False``.
        """
        user_id = verify_admin_token(token)
        if user_id is None:
            try:
                await websocket.close(code=4401)
            except Exception:
                pass
            return False

        await websocket.accept()
        async with self._lock:
            self._clients.add(websocket)
        # Ensure the broadcaster is running
        await self.start()
        # Send initial snapshot immediately
        try:
            metrics = await get_metrics_collector().collect_current()
            await websocket.send_text(json.dumps({"type": "metric", "metrics": metrics}))
        except Exception:
            pass
        _log.info("metrics_ws_client_connected", user_id=user_id,
                  client_count=len(self._clients))
        return True

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(websocket)
        try:
            if websocket.application_state != WebSocketState.DISCONNECTED:
                await websocket.close()
        except Exception:
            pass

    async def broadcast(self, msg: dict[str, Any]) -> None:
        data = json.dumps(msg, default=str)
        dead: list[WebSocket] = []
        async with self._lock:
            clients = list(self._clients)
        for ws in clients:
            try:
                if ws.application_state == WebSocketState.CONNECTED:
                    await ws.send_text(data)
                else:
                    dead.append(ws)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self._clients.discard(ws)

    # ── Internal loop ───────────────────────────────────────────────────

    async def _broadcast_loop(self) -> None:
        collector = get_metrics_collector()
        try:
            while self._running:
                start = time.monotonic()
                try:
                    metrics = await collector.collect_current()
                    if self._clients:
                        await self.broadcast({"type": "metric", "metrics": metrics})

                    # Forward any new alerts since last tick
                    alerts = metrics.get("alerts") or []
                    if alerts and self._clients:
                        latest = alerts[-1]
                        ts = float(latest.get("timestamp", 0.0))
                        if ts > self._last_alert_ts:
                            self._last_alert_ts = ts
                            await self.broadcast({"type": "alert", "alert": latest})
                except Exception as exc:
                    _log.error("metrics_ws_loop_error", error=str(exc))

                elapsed = time.monotonic() - start
                await asyncio.sleep(max(0.0, 1.0 - elapsed))
        except asyncio.CancelledError:
            raise


# ── Singleton accessor ──────────────────────────────────────────────────

_INSTANCE: Optional[MetricsWebSocketManager] = None


def get_ws_manager() -> MetricsWebSocketManager:
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = MetricsWebSocketManager()
    return _INSTANCE
