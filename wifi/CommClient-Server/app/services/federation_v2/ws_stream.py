"""
FederationWebSocketManager — fan-out broadcaster for the Federation
Health-Map admin panel.

The admin panel subscribes to ``/api/admin/federation/ws/federation``
and receives JSON frames whose ``type`` is one of:

    handshake        — handshake completed / failed
    sync             — sync milestone / failure
    conflict         — DAG conflict surfaced
    partition        — peer down / recovered
    role_change      — master ↔ follower transition
    shaper_change    — bandwidth rule changed
    cert             — cert rotation / expiry warning
    quorum           — leader / election / split-brain
    policy           — policy CRUD
    diagnostic       — diagnose result
    heartbeat        — every ``HEARTBEAT_SEC`` when idle

Auth: ``?token=<jwt>`` query-string (admin role required), close
codes mirror the topology manager (4401/4403).

Singleton: ``get_ws_manager()``.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import time
from typing import Any, Optional

import structlog
from fastapi import WebSocket, WebSocketDisconnect

from app.core.security import decode_token

logger = structlog.get_logger(__name__)


HEARTBEAT_SEC = 15.0
SEND_TIMEOUT_SEC = 5.0
WS_CODE_AUTH_FAILED = 4401
WS_CODE_FORBIDDEN = 4403


VALID_FRAMES = {
    "handshake", "sync", "conflict", "partition",
    "role_change", "shaper_change", "cert", "quorum",
    "policy", "diagnostic", "heartbeat",
}


class _Subscriber:
    __slots__ = ("ws", "user_id", "queue", "alive")

    def __init__(self, ws: WebSocket, user_id: str) -> None:
        self.ws = ws
        self.user_id = user_id
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=256)
        self.alive = True


class FederationWebSocketManager:
    def __init__(self) -> None:
        self._subs: set[_Subscriber] = set()
        self._lock = asyncio.Lock()
        self._hb_task: Optional[asyncio.Task[None]] = None

    # ── public broadcast API ─────────────────────────────────

    async def broadcast(self, kind: str, payload: dict[str, Any]) -> None:
        if kind not in VALID_FRAMES:
            logger.info("fedmap_ws_unknown_frame", kind=kind)
        frame = {"type": kind, "ts": time.time(), **payload}
        msg = json.dumps(frame, default=str)
        async with self._lock:
            for s in list(self._subs):
                if not s.alive:
                    continue
                try:
                    s.queue.put_nowait(msg)
                except asyncio.QueueFull:
                    # Drop-oldest policy
                    with contextlib.suppress(Exception):
                        _ = s.queue.get_nowait()
                    with contextlib.suppress(Exception):
                        s.queue.put_nowait(msg)

    # ── handler ──────────────────────────────────────────────

    async def handle_connection(self, ws: WebSocket) -> None:
        # Auth
        token = ws.query_params.get("token") or ""
        if not token:
            auth = ws.headers.get("authorization") or ""
            if auth.lower().startswith("bearer "):
                token = auth.split(" ", 1)[1]
        if not token:
            await ws.close(code=WS_CODE_AUTH_FAILED)
            return
        try:
            payload = decode_token(token)
        except Exception:
            await ws.close(code=WS_CODE_AUTH_FAILED)
            return
        if payload.get("type") != "access":
            await ws.close(code=WS_CODE_AUTH_FAILED)
            return
        if payload.get("role") != "admin":
            await ws.close(code=WS_CODE_FORBIDDEN)
            return

        await ws.accept()
        sub = _Subscriber(ws, str(payload.get("sub") or ""))
        async with self._lock:
            self._subs.add(sub)
            if self._hb_task is None or self._hb_task.done():
                self._hb_task = asyncio.create_task(
                    self._heartbeat_loop(), name="fedmap-ws-hb"
                )

        try:
            await self._sender_loop(sub)
        finally:
            async with self._lock:
                self._subs.discard(sub)
            with contextlib.suppress(Exception):
                await ws.close()

    # ── loops ────────────────────────────────────────────────

    async def _sender_loop(self, sub: _Subscriber) -> None:
        try:
            while sub.alive:
                msg = await sub.queue.get()
                try:
                    await asyncio.wait_for(
                        sub.ws.send_text(msg), timeout=SEND_TIMEOUT_SEC,
                    )
                except (asyncio.TimeoutError, WebSocketDisconnect):
                    sub.alive = False
                    return
        except Exception as exc:
            logger.warning("fedmap_ws_sender_error", error=str(exc))
            sub.alive = False

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_SEC)
            async with self._lock:
                if not self._subs:
                    return
            await self.broadcast("heartbeat", {})


# ── singleton ───────────────────────────────────────────────


_mgr: Optional[FederationWebSocketManager] = None


def get_ws_manager() -> FederationWebSocketManager:
    global _mgr
    if _mgr is None:
        _mgr = FederationWebSocketManager()
    return _mgr
