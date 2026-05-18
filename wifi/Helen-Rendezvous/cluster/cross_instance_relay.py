"""
CrossInstanceRelay — forwards request/response frames between Rendezvous
instances over the shared pub/sub bus.

Wire protocol (JSON on `rendezvous:events`):

    {
        "v": 1,
        "msg_id": "<uuid>",
        "from_instance": "<id>",
        "to_instance":   "<id>" | null,   # null = broadcast
        "kind": "tunnel_request" | "tunnel_response" | "ws_open" | "ws_frame"
              | "ws_close" | "ping" | "instance_announce" | "shutdown",
        "peer_id": "<public_id>",
        "payload": {...}
    }

A relay on instance B that receives a tunnel HTTP request for `peer_id`:
    1. Looks up affinity -> owner = instance A.
    2. Calls `send_request(to_instance=A, peer_id, payload=request_frame)`.
    3. Awaits a Future keyed on `msg_id`.
    4. Instance A subscribes, sees the request, hands it to its local
       TunnelEntry, sends a tunnel_response back over the same channel.
    5. Instance B's pump finds the matching `msg_id`, resolves the Future.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from typing import Any, Optional

import structlog

from storage.backend import StorageBackend

logger = structlog.get_logger(__name__)


PROTOCOL_VERSION = 1
DEFAULT_CHANNEL = "rendezvous:events"
DEFAULT_RPC_TIMEOUT_SEC = 30.0


class CrossInstanceRelay:
    """Multiplexed RPC + fire-and-forget messaging over pub/sub."""

    def __init__(
        self,
        backend: StorageBackend,
        instance_id: str,
        *,
        channel: str = DEFAULT_CHANNEL,
        default_timeout: float = DEFAULT_RPC_TIMEOUT_SEC,
    ) -> None:
        self._backend = backend
        self._instance_id = instance_id
        self._channel = channel
        self._default_timeout = default_timeout
        self._pump_task: Optional[asyncio.Task[None]] = None
        self._pending: dict[str, asyncio.Future] = {}
        self._handlers: dict[str, list[Any]] = {}
        self._running = False
        self._stats = {
            "sent": 0,
            "received": 0,
            "responses_matched": 0,
            "unhandled": 0,
        }

    @property
    def instance_id(self) -> str:
        return self._instance_id

    @property
    def channel(self) -> str:
        return self._channel

    @property
    def stats(self) -> dict[str, int]:
        return dict(self._stats)

    # ── Lifecycle ──────────────────────────────────────────

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._pump_task = asyncio.create_task(
            self._pump(),
            name=f"xrelay-{self._instance_id}",
        )
        await self.announce_self()
        logger.info(
            "cross_instance_relay_started",
            instance_id=self._instance_id,
            channel=self._channel,
        )

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        # Best-effort shutdown announce.
        with contextlib.suppress(Exception):
            await self._send(
                kind="shutdown",
                to_instance=None,
                peer_id="",
                payload={},
            )
        if self._pump_task is not None:
            self._pump_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._pump_task
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(RuntimeError("relay shutting down"))
        self._pending.clear()
        logger.info("cross_instance_relay_stopped", instance_id=self._instance_id)

    # ── Handlers ───────────────────────────────────────────

    def on(self, kind: str, handler: Any) -> None:
        """Register an async handler. Signature: `async fn(envelope) -> None`."""
        self._handlers.setdefault(kind, []).append(handler)

    # ── Sends ──────────────────────────────────────────────

    async def announce_self(self) -> None:
        await self._send(
            kind="instance_announce",
            to_instance=None,
            peer_id="",
            payload={"announced_at": time.time()},
        )

    async def fire(
        self,
        kind: str,
        to_instance: Optional[str],
        peer_id: str,
        payload: dict[str, Any],
    ) -> int:
        """Send-and-forget. Returns Redis fan-out count if known."""
        return await self._send(kind, to_instance, peer_id, payload)

    async def request(
        self,
        kind: str,
        to_instance: str,
        peer_id: str,
        payload: dict[str, Any],
        *,
        timeout: Optional[float] = None,
    ) -> dict[str, Any]:
        """Issue an RPC over pub/sub. Returns the matching response payload."""
        msg_id = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[msg_id] = fut
        try:
            await self._send(
                kind=kind,
                to_instance=to_instance,
                peer_id=peer_id,
                payload=payload,
                msg_id=msg_id,
                expects_response=True,
            )
            return await asyncio.wait_for(
                fut,
                timeout=timeout or self._default_timeout,
            )
        finally:
            self._pending.pop(msg_id, None)

    async def respond(
        self,
        msg_id: str,
        to_instance: str,
        peer_id: str,
        payload: dict[str, Any],
        *,
        kind: str = "tunnel_response",
    ) -> int:
        return await self._send(
            kind=kind,
            to_instance=to_instance,
            peer_id=peer_id,
            payload=payload,
            msg_id=msg_id,
            is_response=True,
        )

    async def _send(
        self,
        kind: str,
        to_instance: Optional[str],
        peer_id: str,
        payload: dict[str, Any],
        *,
        msg_id: Optional[str] = None,
        expects_response: bool = False,
        is_response: bool = False,
    ) -> int:
        envelope: dict[str, Any] = {
            "v": PROTOCOL_VERSION,
            "msg_id": msg_id or uuid.uuid4().hex,
            "from_instance": self._instance_id,
            "to_instance": to_instance,
            "kind": kind,
            "peer_id": peer_id,
            "payload": payload,
            "ts": time.time(),
        }
        if expects_response:
            envelope["expects_response"] = True
        if is_response:
            envelope["is_response"] = True
        n = await self._backend.publish_event(self._channel, envelope)
        self._stats["sent"] += 1
        return n

    # ── Pump ───────────────────────────────────────────────

    async def _pump(self) -> None:
        backoff = 0.5
        while self._running:
            try:
                subscription = self._backend.subscribe_events(self._channel)
                try:
                    async for envelope in subscription:
                        await self._dispatch(envelope)
                finally:
                    aclose = getattr(subscription, "aclose", None)
                    if aclose is not None:
                        with contextlib.suppress(Exception):
                            await aclose()
                backoff = 0.5
            except asyncio.CancelledError:
                return
            except Exception as exc:
                if not self._running:
                    return
                logger.warning(
                    "cross_instance_pump_error",
                    error=str(exc),
                    backoff=backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 10.0)

    async def _dispatch(self, envelope: dict[str, Any]) -> None:
        if not isinstance(envelope, dict):
            return
        from_instance = envelope.get("from_instance")
        if from_instance == self._instance_id:
            return  # ignore our own broadcasts
        to_instance = envelope.get("to_instance")
        if to_instance is not None and to_instance != self._instance_id:
            return  # not for us
        self._stats["received"] += 1
        kind = str(envelope.get("kind") or "")
        if envelope.get("is_response"):
            msg_id = str(envelope.get("msg_id") or "")
            fut = self._pending.pop(msg_id, None)
            if fut is not None and not fut.done():
                fut.set_result(envelope.get("payload") or {})
                self._stats["responses_matched"] += 1
            return
        handlers = self._handlers.get(kind, [])
        if not handlers:
            self._stats["unhandled"] += 1
            return
        for h in handlers:
            try:
                await h(envelope)
            except Exception as exc:  # pragma: no cover
                logger.warning(
                    "cross_instance_handler_error",
                    kind=kind,
                    error=str(exc),
                )
