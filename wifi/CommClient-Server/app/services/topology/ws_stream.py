"""
TopologyWebSocketManager — fan-out broadcaster for the topology stream.

The admin Topology Visualizer subscribes to ``/api/admin/ws/topology`` and
expects to receive a steady stream of JSON frames:

    {"type": "graph.full",  "graph": {...}}            on connect
    {"type": "node.update", "node":  {...}}
    {"type": "node.delete", "node_id": "..."}
    {"type": "link.update", "link":  {...}}
    {"type": "link.delete", "src": "...", "dst": "..."}
    {"type": "event",       "event": "...", "data": {...}}
    {"type": "action.update", "job": {...}}            from TopologyActions
    {"type": "heartbeat",   "ts": <epoch>}             every 15 s when idle

The manager:

* authenticates each connection via ``?token=`` query param (admin role
  required), closes with 4401/4403 otherwise;
* runs a periodic re-broadcast loop that rebuilds the aggregated graph
  every ``GRAPH_REFRESH_SEC`` and pushes a ``graph.full`` frame to all
  subscribers — this is the cheapest way to keep the UI converged even when
  individual events are dropped;
* exposes a ``broadcast(kind, payload)`` coroutine that other services
  (federation, overlay, p2p) can call when they emit relevant events;
* hooks into ``TopologyActions`` so action job updates flow through the
  same WS stream.

Implementation is intentionally similar to ``services.monitoring.ws_streamer``
to keep the operator mental model identical across panels.
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
from app.services.topology.aggregator import (
    TopologyGraph,
    get_topology_aggregator,
)

logger = structlog.get_logger(__name__)


# ─────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────

GRAPH_REFRESH_SEC = 5.0
HEARTBEAT_SEC     = 15.0
SEND_TIMEOUT_SEC  = 5.0

WS_CODE_AUTH_FAILED   = 4401
WS_CODE_FORBIDDEN     = 4403
WS_CODE_SERVER_ERROR  = 4500


# ─────────────────────────────────────────────────────────────
# Manager
# ─────────────────────────────────────────────────────────────


class _Subscriber:
    """A single connected admin client."""

    __slots__ = ("ws", "user_id", "queue", "alive", "subscribed_at")

    def __init__(self, ws: WebSocket, user_id: str) -> None:
        self.ws = ws
        self.user_id = user_id
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=512)
        self.alive = True
        self.subscribed_at = time.time()


class TopologyWebSocketManager:
    """Singleton broadcaster."""

    _singleton: "TopologyWebSocketManager | None" = None

    def __init__(self) -> None:
        self._subs: list[_Subscriber] = []
        self._lock = asyncio.Lock()
        self._refresh_task: Optional[asyncio.Task[None]] = None
        self._started = False

    @classmethod
    def instance(cls) -> "TopologyWebSocketManager":
        if cls._singleton is None:
            cls._singleton = TopologyWebSocketManager()
        return cls._singleton

    # ── Lifecycle ─────────────────────────────────────────────

    async def ensure_started(self) -> None:
        if self._started:
            return
        self._started = True
        self._refresh_task = asyncio.create_task(
            self._refresh_loop(), name="topology-ws-refresh",
        )
        # Wire ourselves to the actions service so job updates broadcast too.
        try:
            from app.services.topology.actions import get_topology_actions
            get_topology_actions().set_broadcaster(self.broadcast)
        except Exception as e:  # pragma: no cover — wiring is optional
            logger.debug("topology_ws_actions_wire_failed", error=str(e))

    async def shutdown(self) -> None:
        self._started = False
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            with contextlib.suppress(Exception):
                await self._refresh_task
        async with self._lock:
            for s in list(self._subs):
                s.alive = False
                with contextlib.suppress(Exception):
                    await s.ws.close()
            self._subs.clear()

    # ── Connection handling ───────────────────────────────────

    async def handle_connection(self, ws: WebSocket) -> None:
        """Accept, authenticate, register, and serve a connection until close."""
        await ws.accept()
        user_id = await self._authenticate(ws)
        if user_id is None:
            return
        await self.ensure_started()

        sub = _Subscriber(ws=ws, user_id=user_id)
        async with self._lock:
            self._subs.append(sub)

        try:
            # Send the initial full graph snapshot so the client paints fast.
            try:
                graph = await get_topology_aggregator().build_graph()
                await self._send(sub, {
                    "type": "graph.full",
                    "graph": graph.to_dict(),
                })
            except Exception as e:
                logger.warning("topology_ws_initial_snapshot_failed", error=str(e))

            # Run a writer + reader concurrently.
            writer_task = asyncio.create_task(self._writer(sub))
            reader_task = asyncio.create_task(self._reader(sub))

            done, pending = await asyncio.wait(
                {writer_task, reader_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
                with contextlib.suppress(Exception):
                    await t
        except WebSocketDisconnect:
            pass
        except Exception as e:
            logger.warning(
                "topology_ws_error",
                user_id=user_id,
                error=str(e),
            )
        finally:
            sub.alive = False
            async with self._lock:
                with contextlib.suppress(ValueError):
                    self._subs.remove(sub)

    async def _authenticate(self, ws: WebSocket) -> Optional[str]:
        token = (
            ws.query_params.get("token")
            or ws.query_params.get("access_token")
        )
        if not token:
            auth = ws.headers.get("authorization") or ""
            if auth.lower().startswith("bearer "):
                token = auth.split(" ", 1)[1].strip()
        if not token:
            with contextlib.suppress(Exception):
                await ws.close(code=WS_CODE_AUTH_FAILED)
            return None
        try:
            payload = decode_token(token)
        except Exception:
            with contextlib.suppress(Exception):
                await ws.close(code=WS_CODE_AUTH_FAILED)
            return None
        if payload.get("type") != "access":
            with contextlib.suppress(Exception):
                await ws.close(code=WS_CODE_AUTH_FAILED)
            return None
        if payload.get("role") != "admin":
            with contextlib.suppress(Exception):
                await ws.close(code=WS_CODE_FORBIDDEN)
            return None
        return payload.get("sub")

    # ── Reader / writer ───────────────────────────────────────

    async def _reader(self, sub: _Subscriber) -> None:
        while sub.alive:
            try:
                msg = await sub.ws.receive_text()
            except WebSocketDisconnect:
                sub.alive = False
                return
            except Exception:
                sub.alive = False
                return
            # Minimal client protocol: ping/refresh/subscribe.
            try:
                cmd = json.loads(msg)
            except json.JSONDecodeError:
                continue
            kind = cmd.get("type")
            if kind == "ping":
                await self._send(sub, {"type": "pong", "ts": time.time()})
            elif kind == "refresh":
                with contextlib.suppress(Exception):
                    graph = await get_topology_aggregator().build_graph(
                        force_refresh=True,
                    )
                    await self._send(sub, {
                        "type":  "graph.full",
                        "graph": graph.to_dict(),
                    })

    async def _writer(self, sub: _Subscriber) -> None:
        while sub.alive:
            try:
                frame = await asyncio.wait_for(sub.queue.get(), timeout=HEARTBEAT_SEC)
            except asyncio.TimeoutError:
                # Idle heartbeat.
                await self._send_raw(sub, json.dumps({
                    "type": "heartbeat", "ts": time.time(),
                }))
                continue
            await self._send_raw(sub, frame)

    async def _send(self, sub: _Subscriber, payload: dict[str, Any]) -> None:
        try:
            sub.queue.put_nowait(json.dumps(payload, default=str))
        except asyncio.QueueFull:
            # Slow client — drop the connection rather than block fan-out.
            logger.info("topology_ws_slow_client_dropped",
                        user_id=sub.user_id)
            sub.alive = False
            with contextlib.suppress(Exception):
                await sub.ws.close()

    async def _send_raw(self, sub: _Subscriber, frame: str) -> None:
        try:
            await asyncio.wait_for(sub.ws.send_text(frame), timeout=SEND_TIMEOUT_SEC)
        except Exception:
            sub.alive = False
            with contextlib.suppress(Exception):
                await sub.ws.close()

    # ── Broadcasting ──────────────────────────────────────────

    async def broadcast(self, kind: str, payload: dict[str, Any]) -> None:
        """Push one event to every connected admin client."""
        frame = {"type": kind, **payload, "ts": time.time()}
        async with self._lock:
            subs = list(self._subs)
        for s in subs:
            if not s.alive:
                continue
            try:
                s.queue.put_nowait(json.dumps(frame, default=str))
            except asyncio.QueueFull:
                s.alive = False
                with contextlib.suppress(Exception):
                    await s.ws.close()

    # ── Background refresh ────────────────────────────────────

    async def _refresh_loop(self) -> None:
        """Push a fresh full-graph snapshot every ``GRAPH_REFRESH_SEC``."""
        while True:
            try:
                await asyncio.sleep(GRAPH_REFRESH_SEC)
                async with self._lock:
                    if not self._subs:
                        continue
                graph = await get_topology_aggregator().build_graph()
                await self.broadcast("graph.full", {"graph": graph.to_dict()})
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning("topology_ws_refresh_failed", error=str(e))
                await asyncio.sleep(GRAPH_REFRESH_SEC)

    # ── Diagnostics ──────────────────────────────────────────

    def stats(self) -> dict[str, Any]:
        return {
            "subscribers": len(self._subs),
            "started":     self._started,
        }


def get_topology_ws_manager() -> TopologyWebSocketManager:
    return TopologyWebSocketManager.instance()
