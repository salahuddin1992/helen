"""
MQTT broker adapter — alternate pub/sub backend for inter-server
fanout (alongside Redis Streams default and the NATS option).

Why this exists
---------------
Some operators already run an MQTT broker on their LAN (mosquitto,
HiveMQ CE) for IoT or industrial-control workloads. This adapter
lets Helen reuse that broker as its fanout substrate instead of
spinning up Redis. The wire shape mirrors broker_client.publish()
so route_executor stays backend-agnostic.

Selection
---------
``HELEN_BROKER_BACKEND=mqtt`` plus ``HELEN_MQTT_HOST=10.0.0.5`` (and
optionally ``HELEN_MQTT_PORT=1883``, ``HELEN_MQTT_USERNAME``,
``HELEN_MQTT_PASSWORD``, ``HELEN_MQTT_TLS=1``) makes
``configure_broker()`` in main.py prefer this adapter.

Topic mapping
-------------
Helen subjects use slash-style ``fabric.P0.call.signal.offer.server_037``.
MQTT canonical separator is ``/``; we translate transparently:
``helen/fabric/P0/call/signal/offer/server_037``. Wildcards: ``+`` for
single-level, ``#`` for tail (MQTT semantics, not Helen's).

100% LAN
--------
``HELEN_MQTT_HOST`` must point to an internal IP. This module never
contacts a public MQTT broker.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any, Awaitable, Callable, Optional


logger = logging.getLogger(__name__)


class MQTTNotInstalledError(RuntimeError):
    pass


def _subject_to_topic(subject: str) -> str:
    """Helen's dotted subject → MQTT slash topic, namespaced."""
    return "helen/" + subject.replace(".", "/")


def _topic_to_subject(topic: str) -> str:
    if topic.startswith("helen/"):
        topic = topic[len("helen/"):]
    return topic.replace("/", ".")


class MQTTAdapter:
    """paho-mqtt-based pub/sub. paho's network thread is sync; we
    bridge into asyncio via a per-connection event-loop reference so
    incoming messages are scheduled correctly."""

    def __init__(
        self,
        host: str, port: int = 1883,
        *,
        username: Optional[str] = None,
        password: Optional[str] = None,
        use_tls: bool = False,
        client_id: Optional[str] = None,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.use_tls = use_tls
        self.client_id = client_id
        self._client = None  # paho.mqtt.client.Client
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._handlers: dict[str, list[Callable[[dict], Awaitable[None]]]] = {}
        self._lock = threading.Lock()
        self._connected = asyncio.Event()
        self._connect_failed_reason: Optional[str] = None

    async def connect(self, *, connect_timeout: float = 5.0) -> None:
        if self._client is not None:
            return
        try:
            from paho.mqtt import client as mqtt_client  # type: ignore
        except ImportError as exc:
            raise MQTTNotInstalledError(
                "`paho-mqtt` is not installed. Add `paho-mqtt>=2.0` to "
                "requirements.txt and rebuild Helen-Server, OR keep "
                "the default Redis Streams broker.",
            ) from exc

        self._loop = asyncio.get_running_loop()
        self._client = mqtt_client.Client(
            mqtt_client.CallbackAPIVersion.VERSION2,
            client_id=self.client_id or "",
            clean_session=True,
        )
        if self.username:
            self._client.username_pw_set(self.username, self.password or "")
        if self.use_tls:
            import ssl as _ssl
            ctx = _ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = _ssl.CERT_NONE  # LAN tolerance
            self._client.tls_set_context(ctx)

        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

        # paho's blocking connect happens in a worker thread so we
        # don't block the event loop on a stalled broker.
        await asyncio.get_running_loop().run_in_executor(
            None, lambda: self._client.connect(self.host, self.port, 60),
        )
        self._client.loop_start()
        try:
            await asyncio.wait_for(
                self._connected.wait(), timeout=connect_timeout,
            )
        except asyncio.TimeoutError:
            self._client.loop_stop()
            raise RuntimeError(
                f"MQTT broker at {self.host}:{self.port} did not "
                f"acknowledge CONNECT within {connect_timeout}s",
            )
        if self._connect_failed_reason:
            raise RuntimeError(
                f"MQTT CONNECT refused: {self._connect_failed_reason}",
            )
        logger.info("mqtt_connected host=%s:%d tls=%s",
                    self.host, self.port, self.use_tls)

    async def close(self) -> None:
        if self._client is not None:
            try:
                self._client.loop_stop()
            except Exception:
                pass
            try:
                self._client.disconnect()
            except Exception:
                pass
        self._client = None

    # ── paho callbacks (sync, run on paho's thread) ────────────

    def _on_connect(self, client, _userdata, _flags, rc, _props=None):
        if rc == 0:
            try:
                # asyncio Event must be set on the original loop
                if self._loop is not None:
                    self._loop.call_soon_threadsafe(self._connected.set)
            except RuntimeError:
                pass
        else:
            self._connect_failed_reason = f"rc={rc}"
            try:
                if self._loop is not None:
                    self._loop.call_soon_threadsafe(self._connected.set)
            except RuntimeError:
                pass

    def _on_disconnect(self, _client, _userdata, _rc, _props=None, _reason=None):
        self._connected.clear()
        logger.info("mqtt_disconnected host=%s:%d", self.host, self.port)

    def _on_message(self, _client, _userdata, msg) -> None:
        # Called on paho's thread; schedule the handler on our loop.
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except Exception as exc:
            logger.warning("mqtt_decode_failed topic=%s error=%s",
                           msg.topic, exc)
            return
        with self._lock:
            handlers = list(self._handlers.get(msg.topic, []))
        if not handlers:
            return
        for h in handlers:
            try:
                if self._loop is not None:
                    asyncio.run_coroutine_threadsafe(h(payload), self._loop)
            except Exception as exc:
                logger.warning("mqtt_handler_dispatch_failed error=%s", exc)

    # ── pub/sub ────────────────────────────────────────────────

    async def publish(self, subject: str, payload: dict, *, qos: int = 0) -> None:
        if self._client is None:
            raise RuntimeError("MQTT adapter not connected")
        topic = _subject_to_topic(subject)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        info = self._client.publish(topic, body, qos=qos)
        # paho returns immediately; check error code
        if info.rc != 0:
            raise RuntimeError(f"MQTT publish failed rc={info.rc}")

    async def subscribe(
        self,
        subject: str,
        handler: Callable[[dict], Awaitable[None]],
        *,
        qos: int = 0,
    ) -> None:
        if self._client is None:
            raise RuntimeError("MQTT adapter not connected")
        topic = _subject_to_topic(subject)
        with self._lock:
            self._handlers.setdefault(topic, []).append(handler)
        result, _mid = self._client.subscribe(topic, qos=qos)
        if result != 0:
            raise RuntimeError(f"MQTT subscribe failed rc={result}")
        logger.info("mqtt_subscribed topic=%s", topic)

    # ── stats ──────────────────────────────────────────────────

    def stats(self) -> dict[str, Any]:
        return {
            "connected": self._connected.is_set(),
            "host": self.host,
            "port": self.port,
            "tls": self.use_tls,
            "topics_subscribed": len(self._handlers),
        }


# ── Module-level singleton ─────────────────────────────────────────


_INSTANCE: Optional[MQTTAdapter] = None


async def configure_mqtt(
    host: str, port: int = 1883, *,
    username: Optional[str] = None,
    password: Optional[str] = None,
    use_tls: bool = False,
    client_id: Optional[str] = None,
) -> MQTTAdapter:
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = MQTTAdapter(
            host=host, port=port,
            username=username, password=password,
            use_tls=use_tls, client_id=client_id,
        )
        await _INSTANCE.connect()
    return _INSTANCE


def get_mqtt() -> Optional[MQTTAdapter]:
    return _INSTANCE


async def shutdown_mqtt() -> None:
    global _INSTANCE
    if _INSTANCE is not None:
        await _INSTANCE.close()
        _INSTANCE = None
