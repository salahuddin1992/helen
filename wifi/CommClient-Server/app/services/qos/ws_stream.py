"""
QoS WebSocket fan-out — pushes live snapshots to subscribed admin clients.

Protocol
--------
Client connects to ``/api/admin/ws/qos?token=<jwt>`` and sends frames:

  { "op": "subscribe",   "call_id": "<id>" }
  { "op": "unsubscribe", "call_id": "<id>" }
  { "op": "subscribe_global" }

The server pushes at most one frame per call per ``tick_interval`` seconds:

  {
    "type": "qos_tick",
    "call_id": "<id>",
    "ts": 1715512345.78,
    "latest": { "<pid>": { "streams": {...}, "mos_avg": <float> } },
    "summary": { "active_streams": ..., "mos_avg": ... },
    "anomalies": [ ... ]
  }

A single global broadcaster task runs on the FastAPI event loop; per-client
state is kept in a lightweight ``Subscription`` object.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from app.core.logging import get_logger
from app.services.qos.anomaly_detector import qos_anomaly_detector
from app.services.qos.stats_collector import qos_stats_collector

logger = get_logger(__name__)


DEFAULT_TICK_INTERVAL_S = 1.0           # 1 Hz — bump to 0.5 for 2 Hz


@dataclass
class Subscription:
    websocket: WebSocket
    user_id: str
    call_ids: set[str] = field(default_factory=set)
    subscribed_global: bool = False
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=64))
    closed: bool = False


class QoSWebSocketManager:
    """
    Multiplexed per-call fan-out.

    Design choices:
      * One background broadcaster task — keeps timing consistent across
        every subscriber and avoids N tasks for N clients.
      * Per-client ``asyncio.Queue`` so slow consumers don't backpressure
        the broadcaster; if the queue fills we drop frames (newest wins).
      * Token verification happens in the route handler before ``accept``;
        the manager only sees authenticated subscriptions.
    """

    def __init__(self, tick_interval: float = DEFAULT_TICK_INTERVAL_S) -> None:
        self._subs: list[Subscription] = []
        self._lock = asyncio.Lock()
        self._tick = tick_interval
        self._task: asyncio.Task | None = None
        self._running = False

    # ── Lifecycle ──────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._broadcast_loop(), name="qos_ws_broadcaster")
        logger.info("qos_ws_broadcaster_started", interval=self._tick)

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        async with self._lock:
            for sub in self._subs:
                try:
                    await sub.websocket.close()
                except Exception:
                    pass
            self._subs.clear()

    # ── Client session driver ──────────────────────────────────────────

    async def serve(self, websocket: WebSocket, user_id: str) -> None:
        """
        Drive one WebSocket end-to-end. Caller must have ``accept()``ed
        the connection before invoking this.
        """
        sub = Subscription(websocket=websocket, user_id=user_id)
        async with self._lock:
            self._subs.append(sub)
        await self.start()

        sender = asyncio.create_task(self._sender_loop(sub),
                                     name=f"qos_ws_sender:{user_id[:8]}")
        try:
            while True:
                msg = await websocket.receive_json()
                await self._handle_client_msg(sub, msg)
        except WebSocketDisconnect:
            pass
        except Exception as e:                       # pragma: no cover
            logger.warning("qos_ws_recv_error", user_id=user_id, error=str(e))
        finally:
            sub.closed = True
            sender.cancel()
            async with self._lock:
                if sub in self._subs:
                    self._subs.remove(sub)
            try:
                await websocket.close()
            except Exception:
                pass

    async def _handle_client_msg(self, sub: Subscription, msg: dict[str, Any]) -> None:
        op = (msg or {}).get("op")
        if op == "subscribe":
            cid = msg.get("call_id")
            if cid:
                sub.call_ids.add(cid)
        elif op == "unsubscribe":
            cid = msg.get("call_id")
            if cid:
                sub.call_ids.discard(cid)
        elif op == "subscribe_global":
            sub.subscribed_global = True
        elif op == "unsubscribe_global":
            sub.subscribed_global = False
        elif op == "ping":
            await self._enqueue(sub, {"type": "pong", "ts": time.time()})

    # ── Sender + broadcaster ──────────────────────────────────────────

    async def _sender_loop(self, sub: Subscription) -> None:
        try:
            while not sub.closed:
                frame = await sub.queue.get()
                if frame is None:
                    break
                await sub.websocket.send_json(frame)
        except WebSocketDisconnect:
            sub.closed = True
        except Exception as e:                       # pragma: no cover
            logger.debug("qos_ws_send_error", user_id=sub.user_id, error=str(e))
            sub.closed = True

    async def _broadcast_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self._tick)
                await self._tick_once()
            except asyncio.CancelledError:
                break
            except Exception as e:                   # pragma: no cover
                logger.warning("qos_ws_tick_error", error=str(e))

    async def _tick_once(self) -> None:
        async with self._lock:
            subs = list(self._subs)

        if not subs:
            return

        # Pre-compute snapshots only for call_ids someone cares about.
        wanted: set[str] = set()
        any_global = False
        for s in subs:
            wanted.update(s.call_ids)
            any_global = any_global or s.subscribed_global

        snaps: dict[str, dict] = {}
        for cid in wanted:
            snaps[cid] = {
                "type": "qos_tick",
                "call_id": cid,
                "ts": time.time(),
                "latest": qos_stats_collector.latest_per_participant(cid),
                "anomalies": [a.to_dict() for a in qos_anomaly_detector.detect(cid)],
            }

        global_frame: dict | None = None
        if any_global:
            global_frame = {
                "type": "qos_global",
                "ts": time.time(),
                "summary": qos_stats_collector.aggregate_summary(),
                "anomalies": {
                    cid: [a.to_dict() for a in anns]
                    for cid, anns in qos_anomaly_detector.detect_all().items()
                },
            }

        for sub in subs:
            for cid in sub.call_ids:
                frame = snaps.get(cid)
                if frame is not None:
                    await self._enqueue(sub, frame)
            if sub.subscribed_global and global_frame is not None:
                await self._enqueue(sub, global_frame)

    @staticmethod
    async def _enqueue(sub: Subscription, frame: dict) -> None:
        try:
            sub.queue.put_nowait(frame)
        except asyncio.QueueFull:
            # Drop the oldest, then push the newest — fresher data wins.
            try:
                sub.queue.get_nowait()
            except Exception:
                pass
            try:
                sub.queue.put_nowait(frame)
            except Exception:
                pass


qos_ws_manager = QoSWebSocketManager()
