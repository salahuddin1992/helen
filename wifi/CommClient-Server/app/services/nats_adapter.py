"""
NATS messaging adapter — alternate broker backend for inter-server
fanout (mirror of broker_client.py's Redis Streams default).

Why this exists
---------------
Redis Streams is the default broker because Redis is already a Helen
dependency. Some operators run NATS on their LAN (lighter, single
binary, no AOF/RDB tuning) and prefer to use it as the messaging
substrate instead. This module is a drop-in publisher/subscriber that
matches the broker_client interface so route_executor can swap
backends with no code changes.

Selection
---------
``HELEN_BROKER_BACKEND=nats`` plus ``HELEN_NATS_URL=nats://10.0.0.10:4222``
makes ``configure_broker(...)`` in main.py prefer this adapter.
Default remains Redis Streams.

100% LAN
--------
The NATS server runs on a LAN host. This module does NOT contact any
public NATS service. ``HELEN_NATS_URL`` must point to an internal IP.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator, Awaitable, Callable, Optional


logger = logging.getLogger(__name__)


class NATSNotInstalledError(RuntimeError):
    pass


class NATSAdapter:
    """Async pub/sub bound to the NATS protocol.

    The ``publish`` semantics match broker_client:
        await adapter.publish("fabric.P0.call.signal.offer.server_037",
                              envelope)

    For wildcard subscriptions (``fabric.P0.>.server_001``), NATS
    subjects use ``>`` for tail wildcards and ``*`` for one-token
    wildcards — same semantics this module exposes.
    """

    def __init__(self, url: str) -> None:
        self.url = url
        self._nc = None  # nats.aio.client.Client
        self._subs: list = []   # opaque NATS Subscription handles
        self._connected = False

    async def connect(self, *, max_reconnect_attempts: int = -1) -> None:
        """Idempotent — second call is a no-op."""
        if self._connected:
            return
        try:
            import nats  # type: ignore
        except ImportError as exc:
            raise NATSNotInstalledError(
                "`nats-py` is not installed. Add `nats-py>=2.6` to "
                "requirements.txt and rebuild Helen-Server, OR keep "
                "the default Redis Streams broker.",
            ) from exc
        self._nc = await nats.connect(
            self.url,
            allow_reconnect=True,
            max_reconnect_attempts=max_reconnect_attempts,
            connect_timeout=4.0,
        )
        self._connected = True
        logger.info("nats_connected url_prefix=%s", self.url.split("@")[0][:32])

    async def close(self) -> None:
        if self._nc is not None:
            try:
                await self._nc.drain()
            except Exception:
                pass
            try:
                await self._nc.close()
            except Exception:
                pass
        self._connected = False
        self._nc = None
        self._subs.clear()

    # ── pub/sub ────────────────────────────────────────────────

    async def publish(self, subject: str, payload: dict) -> None:
        if not self._connected or self._nc is None:
            raise RuntimeError("NATS adapter not connected")
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        await self._nc.publish(subject, body)

    async def subscribe(
        self,
        subject: str,
        handler: Callable[[dict], Awaitable[None]],
        *,
        queue_group: Optional[str] = None,
    ) -> None:
        """Register an async handler for messages on ``subject``.
        ``queue_group`` enables work-sharing — only one subscriber in
        the group receives each message (load-balanced consumer)."""
        if not self._connected or self._nc is None:
            raise RuntimeError("NATS adapter not connected")

        async def _on_msg(msg) -> None:
            try:
                payload = json.loads(msg.data.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                logger.warning("nats_decode_failed error=%s", exc)
                return
            try:
                await handler(payload)
            except Exception as exc:
                logger.warning(
                    "nats_handler_failed subject=%s error=%s",
                    subject, exc,
                )

        sub = await self._nc.subscribe(
            subject, queue=queue_group, cb=_on_msg,
        )
        self._subs.append(sub)
        logger.info(
            "nats_subscribed subject=%s queue_group=%s",
            subject, queue_group or "<none>",
        )

    async def request(
        self, subject: str, payload: dict, *, timeout: float = 4.0,
    ) -> Optional[dict]:
        """Synchronous-style request/reply — sends and awaits one
        response within ``timeout`` seconds. Returns None on timeout
        or no responder."""
        if not self._connected or self._nc is None:
            raise RuntimeError("NATS adapter not connected")
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            msg = await self._nc.request(subject, body, timeout=timeout)
        except Exception as exc:
            logger.debug("nats_request_failed subject=%s error=%s",
                         subject, exc)
            return None
        try:
            return json.loads(msg.data.decode("utf-8"))
        except Exception:
            return None

    async def stream_iter(self, subject: str) -> AsyncIterator[dict]:
        """Yield messages on ``subject`` as they arrive — useful when
        the caller wants ``async for env in adapter.stream_iter(...)``
        instead of a callback."""
        if not self._connected or self._nc is None:
            raise RuntimeError("NATS adapter not connected")
        queue: asyncio.Queue = asyncio.Queue(maxsize=1024)

        async def _push(msg) -> None:
            try:
                payload = json.loads(msg.data.decode("utf-8"))
            except Exception:
                return
            await queue.put(payload)

        sub = await self._nc.subscribe(subject, cb=_push)
        self._subs.append(sub)
        try:
            while True:
                yield await queue.get()
        finally:
            try:
                await sub.unsubscribe()
            except Exception:
                pass

    # ── stats ──────────────────────────────────────────────────

    def stats(self) -> dict[str, Any]:
        return {
            "connected": self._connected,
            "url_prefix": self.url.split("@")[0][:32] if self.url else "",
            "subscriptions": len(self._subs),
        }


# ── Module-level singleton ─────────────────────────────────────────


_INSTANCE: Optional[NATSAdapter] = None


async def configure_nats(url: str) -> NATSAdapter:
    """Idempotent. Pass an internal LAN URL like
    ``nats://10.0.0.10:4222``. Connects on first call, returns the
    same adapter on subsequent calls."""
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = NATSAdapter(url)
    if not _INSTANCE._connected:
        await _INSTANCE.connect()
    return _INSTANCE


def get_nats() -> Optional[NATSAdapter]:
    return _INSTANCE


async def shutdown_nats() -> None:
    global _INSTANCE
    if _INSTANCE is not None:
        await _INSTANCE.close()
        _INSTANCE = None
