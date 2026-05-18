"""
RabbitMQ messaging adapter — aio-pika-based AMQP wrapper.

Why this exists
---------------
Helen ships with Redis Streams default + NATS, MQTT, ZeroMQ
alternates. RabbitMQ is the right backend when an operator already
runs a corporate AMQP cluster (banking, government, manufacturing —
where AMQP-based message routing has been the standard for two
decades). Using their existing broker means no extra ops surface:
they get HA, monitoring, ACLs from the deployment they already run.

Wire shape
----------
We map Helen subjects to AMQP topic-exchange routing keys:

  Helen subject               AMQP routing key
  ────────────────────────    ──────────────────────────
  fabric.P0.call.signal.x     fabric.P0.call.signal.x
  (dotted notation matches AMQP's native topic syntax exactly)

Wildcard semantics: AMQP topic exchanges use ``*`` (one segment) and
``#`` (multi-segment) — both supported through aio-pika's binding.

Selection
---------
``HELEN_BROKER_BACKEND=rabbitmq`` plus ``HELEN_RABBITMQ_URL=amqp://user:pass@10.0.0.5:5672/``
makes ``configure_broker`` use this adapter. The URL supports vhosts
(``...:5672/helen``) for tenant isolation on shared brokers.

100% LAN
--------
URL must point to an internal AMQP broker. This module never
contacts a public service.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable, Optional


logger = logging.getLogger(__name__)


class RabbitMQNotInstalledError(RuntimeError):
    pass


class RabbitMQAdapter:
    """Async pub/sub bound to an AMQP broker via aio-pika. Uses a
    single topic exchange (default name ``helen.events``) so every
    Helen-Server in the cluster shares the same routing namespace."""

    DEFAULT_EXCHANGE = "helen.events"

    def __init__(
        self,
        url: str,
        *,
        exchange_name: str = DEFAULT_EXCHANGE,
        queue_prefix: str = "helen",
    ) -> None:
        self.url = url
        self.exchange_name = exchange_name
        self.queue_prefix = queue_prefix
        self._connection = None
        self._channel = None
        self._exchange = None
        self._queues: dict[str, object] = {}
        self._connected = False

    async def connect(self) -> None:
        if self._connected:
            return
        try:
            import aio_pika  # type: ignore
        except ImportError as exc:
            raise RabbitMQNotInstalledError(
                "`aio-pika` is not installed. Add `aio-pika>=9.4` to "
                "requirements.txt and rebuild Helen-Server, OR keep "
                "the default Redis Streams broker.",
            ) from exc

        self._aio_pika = aio_pika
        self._connection = await aio_pika.connect_robust(
            self.url, timeout=8.0,
        )
        self._channel = await self._connection.channel()
        await self._channel.set_qos(prefetch_count=64)
        self._exchange = await self._channel.declare_exchange(
            self.exchange_name,
            aio_pika.ExchangeType.TOPIC,
            durable=True,
        )
        self._connected = True
        logger.info("rabbitmq_connected url_prefix=%s exchange=%s",
                    self.url.split("@")[-1][:32], self.exchange_name)

    async def close(self) -> None:
        if self._connection is not None:
            try:
                await self._connection.close()
            except Exception:
                pass
        self._connection = None
        self._channel = None
        self._exchange = None
        self._queues.clear()
        self._connected = False

    # ── pub/sub ────────────────────────────────────────────────

    async def publish(self, subject: str, payload: dict) -> None:
        if not self._connected or self._exchange is None:
            raise RuntimeError("RabbitMQ adapter not connected")
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        msg = self._aio_pika.Message(
            body=body,
            content_type="application/json",
            delivery_mode=self._aio_pika.DeliveryMode.PERSISTENT,
        )
        await self._exchange.publish(msg, routing_key=subject)

    async def subscribe(
        self,
        subject_pattern: str,
        handler: Callable[[dict], Awaitable[None]],
        *,
        queue_name: Optional[str] = None,
    ) -> None:
        """Bind a queue to ``subject_pattern`` (AMQP routing-key syntax —
        ``*`` for a single segment, ``#`` for multi-segment).
        ``queue_name=None`` declares an exclusive auto-named queue
        (each subscriber gets its own copy of every message). Pass a
        shared name to enable competing consumers (work-sharing)."""
        if not self._connected or self._channel is None or \
                self._exchange is None:
            raise RuntimeError("RabbitMQ adapter not connected")

        if queue_name is None:
            qn = ""
            exclusive = True
            durable = False
            auto_delete = True
        else:
            qn = f"{self.queue_prefix}.{queue_name}"
            exclusive = False
            durable = True
            auto_delete = False

        queue = await self._channel.declare_queue(
            qn,
            exclusive=exclusive,
            durable=durable,
            auto_delete=auto_delete,
        )
        await queue.bind(self._exchange, routing_key=subject_pattern)
        self._queues[subject_pattern] = queue

        async def _on_message(msg) -> None:
            async with msg.process(requeue=False):
                try:
                    payload = json.loads(msg.body.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    logger.warning("rabbitmq_decode_failed error=%s", exc)
                    return
                try:
                    await handler(payload)
                except Exception as exc:
                    logger.warning(
                        "rabbitmq_handler_failed pattern=%s error=%s",
                        subject_pattern, exc,
                    )

        await queue.consume(_on_message)
        logger.info(
            "rabbitmq_subscribed pattern=%s queue=%s exclusive=%s",
            subject_pattern, qn or "<auto>", exclusive,
        )

    # ── stats ──────────────────────────────────────────────────

    def stats(self) -> dict[str, Any]:
        return {
            "connected": self._connected,
            "url_prefix": self.url.split("@")[-1][:32] if self.url else "",
            "exchange": self.exchange_name,
            "queues": list(self._queues.keys()),
        }


# ── Module-level singleton ─────────────────────────────────────────


_INSTANCE: Optional[RabbitMQAdapter] = None


async def configure_rabbitmq(
    url: str, *,
    exchange_name: str = RabbitMQAdapter.DEFAULT_EXCHANGE,
) -> RabbitMQAdapter:
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = RabbitMQAdapter(url, exchange_name=exchange_name)
        await _INSTANCE.connect()
    return _INSTANCE


def get_rabbitmq() -> Optional[RabbitMQAdapter]:
    return _INSTANCE


async def shutdown_rabbitmq() -> None:
    global _INSTANCE
    if _INSTANCE is not None:
        await _INSTANCE.close()
        _INSTANCE = None
