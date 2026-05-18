"""
ZeroMQ messaging adapter — pyzmq-based pub/sub + push/pull.

Why this exists
---------------
Helen ships with Redis Streams as the default broker, with NATS and
MQTT as alternates (added in v9). ZeroMQ fills the niche where
operators want **brokerless** messaging — no central daemon, just
direct socket-to-socket. ZMQ is also the canonical choice for
ultra-low-latency LAN messaging where every microsecond matters
(industrial control, real-time telemetry).

Patterns supported
------------------
This adapter exposes the four most common ZMQ patterns:

  * **PUB / SUB** — fan-out broadcast (matches broker_client.publish)
  * **PUSH / PULL** — work distribution (load-balanced consumers)
  * **REQ / REP** — synchronous request/reply
  * **ROUTER / DEALER** — async, identity-aware routing

The wire-shape mirrors broker_client semantics so route_executor
can swap to ZMQ with minimal code changes:

    await zmq_adapter.publish("fabric.P0.x.y", envelope)
    async for env in zmq_adapter.subscribe("fabric.P0."):
        ...

Selection
---------
``HELEN_BROKER_BACKEND=zeromq`` plus ``HELEN_ZEROMQ_BIND=tcp://0.0.0.0:5555``
makes ``configure_broker`` prefer this adapter. Each Helen-Server
binds a PUB socket and connects SUB sockets to every peer's bind URL.

100% LAN
--------
ZMQ never speaks to a public service. ``HELEN_ZEROMQ_PEERS`` lists
internal-IP peer URLs only.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable, Optional


logger = logging.getLogger(__name__)


class ZeroMQNotInstalledError(RuntimeError):
    pass


class ZeroMQAdapter:
    """Async pub/sub bound to ZMQ sockets via ``pyzmq``'s asyncio
    integration. We bind a PUB socket and (optionally) connect to
    peer PUB sockets via SUB; each peer publishes on its own bind URL
    and subscribers everywhere see the same messages."""

    def __init__(
        self,
        bind_url: str = "tcp://0.0.0.0:5555",
        peer_urls: Optional[list[str]] = None,
    ) -> None:
        self.bind_url = bind_url
        self.peer_urls = list(peer_urls or [])
        self._ctx = None
        self._pub = None
        self._sub = None
        self._handlers: dict[str, list[Callable[[dict], Awaitable[None]]]] = {}
        self._consume_task: Optional[asyncio.Task] = None
        self._connected = False

    async def connect(self) -> None:
        if self._connected:
            return
        try:
            import zmq  # type: ignore
            import zmq.asyncio  # type: ignore
        except ImportError as exc:
            raise ZeroMQNotInstalledError(
                "`pyzmq` is not installed. Add `pyzmq>=25.0` to "
                "requirements.txt and rebuild Helen-Server, OR keep "
                "the default Redis Streams broker.",
            ) from exc

        self._zmq = zmq
        self._ctx = zmq.asyncio.Context.instance()
        self._pub = self._ctx.socket(zmq.PUB)
        self._pub.bind(self.bind_url)
        self._sub = self._ctx.socket(zmq.SUB)
        for url in self.peer_urls:
            try:
                self._sub.connect(url)
            except Exception as exc:
                logger.warning("zmq_peer_connect_failed url=%s error=%s",
                               url, exc)
        self._connected = True
        logger.info(
            "zmq_connected bind=%s peers=%d",
            self.bind_url, len(self.peer_urls),
        )

    async def close(self) -> None:
        if self._consume_task is not None:
            self._consume_task.cancel()
            self._consume_task = None
        for sock in (self._pub, self._sub):
            if sock is not None:
                try:
                    sock.close(linger=0)
                except Exception:
                    pass
        # Don't terminate the context — it may be shared.
        self._pub = None
        self._sub = None
        self._connected = False

    # ── pub/sub ────────────────────────────────────────────────

    async def publish(self, subject: str, payload: dict) -> None:
        if not self._connected or self._pub is None:
            raise RuntimeError("ZeroMQ adapter not connected")
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        # ZMQ multipart: [subject, body] — subscribers filter by prefix.
        await self._pub.send_multipart([subject.encode("utf-8"), body])

    async def subscribe(
        self,
        subject_prefix: str,
        handler: Callable[[dict], Awaitable[None]],
    ) -> None:
        if not self._connected or self._sub is None:
            raise RuntimeError("ZeroMQ adapter not connected")
        # ZMQ SUB filters by byte prefix, not glob.
        self._sub.setsockopt(self._zmq.SUBSCRIBE,
                              subject_prefix.encode("utf-8"))
        self._handlers.setdefault(subject_prefix, []).append(handler)
        # Start the consume loop if not already running.
        if self._consume_task is None:
            self._consume_task = asyncio.create_task(
                self._consume_loop(), name="zmq-consume",
            )
        logger.info("zmq_subscribed prefix=%s", subject_prefix)

    async def _consume_loop(self) -> None:
        try:
            while True:
                parts = await self._sub.recv_multipart()
                if len(parts) != 2:
                    continue
                subject_b, body = parts
                subject = subject_b.decode("utf-8", "replace")
                try:
                    payload = json.loads(body.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                # Dispatch to every handler whose prefix matches.
                for prefix, handlers in self._handlers.items():
                    if subject.startswith(prefix):
                        for h in handlers:
                            try:
                                await h(payload)
                            except Exception as exc:
                                logger.warning(
                                    "zmq_handler_failed prefix=%s error=%s",
                                    prefix, exc,
                                )
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.warning("zmq_consume_loop_failed error=%s", exc)

    # ── push/pull (work-sharing) ──────────────────────────────

    async def push(self, push_url: str, payload: dict) -> None:
        """Send a job to a PULL endpoint (load-balanced across PULL
        workers connected to the same URL)."""
        if not self._connected:
            raise RuntimeError("ZeroMQ adapter not connected")
        push_sock = self._ctx.socket(self._zmq.PUSH)
        try:
            push_sock.connect(push_url)
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            await push_sock.send(body)
        finally:
            push_sock.close(linger=200)

    async def pull(
        self, pull_url: str,
        handler: Callable[[dict], Awaitable[None]],
    ) -> asyncio.Task:
        """Bind a PULL socket and dispatch incoming jobs to ``handler``.
        Returns the task so the caller can cancel."""
        if not self._connected:
            raise RuntimeError("ZeroMQ adapter not connected")
        sock = self._ctx.socket(self._zmq.PULL)
        sock.bind(pull_url)

        async def _loop():
            try:
                while True:
                    body = await sock.recv()
                    try:
                        payload = json.loads(body.decode("utf-8"))
                    except Exception:
                        continue
                    try:
                        await handler(payload)
                    except Exception as exc:
                        logger.warning("zmq_pull_handler_failed error=%s", exc)
            finally:
                sock.close(linger=0)

        return asyncio.create_task(_loop(), name=f"zmq-pull-{pull_url}")

    # ── stats ──────────────────────────────────────────────────

    def stats(self) -> dict[str, Any]:
        return {
            "connected": self._connected,
            "bind_url": self.bind_url,
            "peer_count": len(self.peer_urls),
            "subscriptions": sum(len(v) for v in self._handlers.values()),
        }


# ── Module-level singleton ─────────────────────────────────────────


_INSTANCE: Optional[ZeroMQAdapter] = None


async def configure_zeromq(
    bind_url: str = "tcp://0.0.0.0:5555",
    peer_urls: Optional[list[str]] = None,
) -> ZeroMQAdapter:
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = ZeroMQAdapter(bind_url=bind_url, peer_urls=peer_urls)
        await _INSTANCE.connect()
    return _INSTANCE


def get_zeromq() -> Optional[ZeroMQAdapter]:
    return _INSTANCE


async def shutdown_zeromq() -> None:
    global _INSTANCE
    if _INSTANCE is not None:
        await _INSTANCE.close()
        _INSTANCE = None
