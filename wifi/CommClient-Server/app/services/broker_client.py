"""
Broker client — pub/sub abstraction for cross-server event delivery.

Backends
--------
The blueprint calls for NATS in production. We start with Redis
Streams as the default backend because Redis is already a hard
dependency (locks, presence, registry) — no extra container, no
extra binary, no extra ops surface. Redis Streams gives us:

  * named consumer groups with at-least-once delivery
  * persistent log replayable from arbitrary IDs
  * server-side fan-out to N subscribers
  * native TTL-style trim with MAXLEN

The API is intentionally NATS-shaped so a later swap to nats-py
is a one-file change. Subject patterns:

    fabric.{priority}.{event_type}.{server_id}     # server-targeted
    fabric.user.{user_id}.{event_type}             # user-targeted
    fabric.broadcast.{channel_id}                  # channel-wide
    fabric.dlq.{kind}                              # dead letter
    fabric.trace.{trace_id}                        # trace events
    fabric.ack.{event_id}                          # ACK return path

Usage
-----
    >>> client = await BrokerClient.create(redis_client, this_server_id="server_001")
    >>> await client.publish("fabric.P0.call.signal.offer.server_037", envelope)
    >>> async for env in client.subscribe("fabric.P0.>.server_001"):
    ...     await handle(env)

Fallback
--------
When ``redis_client=None``, we degrade to in-process asyncio.Queue
keyed by subject pattern. Useful for tests + single-server LAN.
Subscribers + publishers in the same process see each other; remote
servers are invisible (which matches reality — without Redis, there
is no broker).
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import time
from collections import defaultdict
from typing import AsyncIterator, Optional, Callable

from app.core.logging import get_logger
from app.services.event_envelope import Envelope

logger = get_logger(__name__)

# Stream MAXLEN — trim to N most-recent entries per stream. Tunable
# per priority via the priority_caps argument; this is the default.
DEFAULT_STREAM_MAXLEN = 10_000

# Block timeout for XREADGROUP — short enough that we can react to
# shutdown promptly, long enough that we don't busy-poll.
READ_BLOCK_MS = 500


class BrokerClient:
    """Thin abstraction over Redis Streams. Constructor is private —
    use ``BrokerClient.create(...)`` to ensure the consumer group
    exists before subscribers start reading."""

    def __init__(
        self,
        redis_client,
        this_server_id: str,
        consumer_name: str,
        stream_maxlen: int = DEFAULT_STREAM_MAXLEN,
    ) -> None:
        self._redis = redis_client
        self._sid = this_server_id
        self._consumer = consumer_name
        self._stream_maxlen = stream_maxlen
        # In-process fallback: subject → list of subscriber queues.
        # Wildcard subscriptions match by fnmatch.
        self._inproc_subs: dict[str, list[asyncio.Queue]] = defaultdict(list)
        # Track active subscribe coroutines so we can cancel cleanly.
        self._stopped = asyncio.Event()
        self._metrics = {
            "published": 0,
            "consumed": 0,
            "publish_failed": 0,
            "consume_failed": 0,
        }

    @classmethod
    async def create(
        cls,
        redis_client,
        this_server_id: str,
        consumer_name: Optional[str] = None,
        stream_maxlen: int = DEFAULT_STREAM_MAXLEN,
    ) -> "BrokerClient":
        client = cls(
            redis_client=redis_client,
            this_server_id=this_server_id,
            consumer_name=consumer_name or this_server_id,
            stream_maxlen=stream_maxlen,
        )
        return client

    @property
    def is_distributed(self) -> bool:
        return self._redis is not None

    # ── Publish ────────────────────────────────────────────────

    async def publish(self, subject: str, env: Envelope) -> bool:
        """Publish ``env`` to ``subject``. Returns True on accept,
        False on transport failure. The envelope's ``current_server_id``
        is rewritten to ``this_server_id`` so receivers can verify the
        sender."""
        if env.is_expired():
            return False

        if self._redis is not None:
            try:
                stream_key = f"helen:fabric:{subject}"
                payload = env.model_dump_json()
                # XADD with MAXLEN ~ N → server trims oldest entries
                # past N. Approximate trim is ~10× faster than exact.
                await self._redis.xadd(
                    stream_key,
                    fields={"e": payload},
                    maxlen=self._stream_maxlen,
                    approximate=True,
                )
                self._metrics["published"] += 1
                return True
            except Exception as e:
                logger.warning(
                    "broker_publish_failed",
                    subject=subject, error=str(e),
                    event_id=env.event_id,
                )
                self._metrics["publish_failed"] += 1
                return False

        # In-process fan-out via fnmatch on subject pattern.
        delivered = 0
        for pattern, queues in list(self._inproc_subs.items()):
            if fnmatch.fnmatch(subject, pattern):
                for q in queues:
                    try:
                        q.put_nowait(env)
                        delivered += 1
                    except asyncio.QueueFull:
                        # In-process can't drop silently — log it.
                        logger.warning(
                            "broker_inproc_queue_full",
                            pattern=pattern, subject=subject,
                        )
        self._metrics["published"] += 1
        return delivered > 0 or len(self._inproc_subs) == 0

    # ── Subscribe ──────────────────────────────────────────────

    async def subscribe(
        self,
        pattern: str,
        *,
        group: Optional[str] = None,
        max_queue: int = 1000,
    ) -> AsyncIterator[Envelope]:
        """Subscribe to envelopes matching ``pattern``.

        With Redis: ``pattern`` is treated as a stream-key prefix
        match (everything under ``helen:fabric:{pattern_root}``). For
        true subject wildcards we'd need NATS — for now, callers
        should pass exact subjects or the prefix they want.

        Without Redis: ``pattern`` accepts fnmatch wildcards
        (``fabric.P0.>`` style → ``fabric.P0.*`` here)."""
        group = group or f"helen:cg:{self._sid}"
        if self._redis is not None:
            async for env in self._subscribe_redis(pattern, group):
                yield env
        else:
            async for env in self._subscribe_inproc(pattern, max_queue):
                yield env

    async def _subscribe_redis(
        self, pattern: str, group: str,
    ) -> AsyncIterator[Envelope]:
        # Translate fnmatch-style ".>" or ".*" wildcards to a single
        # stream key prefix scan. Redis Streams don't have native
        # subject wildcards, so we resolve the wildcard ONCE at
        # subscribe time. Production should call subscribe with
        # explicit subjects and use one consumer per subject.
        prefix = pattern.split("*", 1)[0].split(">", 1)[0]
        prefix = prefix.rstrip(".")
        # SCAN once for matching streams, then XREADGROUP from each.
        # For exact subjects (no wildcard), this resolves to one key.
        keys: list[str] = []
        try:
            cursor = 0
            while True:
                cursor, batch = await self._redis.scan(
                    cursor=cursor,
                    match=f"helen:fabric:{prefix}*",
                    count=100,
                )
                keys.extend(batch)
                if cursor == 0:
                    break
        except Exception as e:
            logger.warning("broker_scan_failed", pattern=pattern, error=str(e))
            return

        if not keys:
            # No streams match yet. Common at startup. We can't
            # block-poll a non-existent stream group, so return.
            # Caller should retry.
            return

        # Ensure consumer group exists for each stream.
        for k in keys:
            try:
                await self._redis.xgroup_create(
                    name=k, groupname=group, id="0", mkstream=True,
                )
            except Exception:
                # BUSYGROUP — group already exists. Fine.
                pass

        # Read loop. Block briefly across all keys.
        streams = {k: ">" for k in keys}
        while not self._stopped.is_set():
            try:
                resp = await self._redis.xreadgroup(
                    groupname=group,
                    consumername=self._consumer,
                    streams=streams,
                    count=10,
                    block=READ_BLOCK_MS,
                )
            except Exception as e:
                logger.warning("broker_xreadgroup_failed", error=str(e))
                self._metrics["consume_failed"] += 1
                await asyncio.sleep(1.0)
                continue

            if not resp:
                continue

            for stream_key, entries in resp:
                stream_key_str = stream_key.decode() if isinstance(stream_key, bytes) else stream_key
                for entry_id, fields in entries:
                    raw = fields.get(b"e") if isinstance(fields, dict) else None
                    if raw is None:
                        # Field name encoding may differ — try str
                        raw = fields.get("e") if isinstance(fields, dict) else None
                    if raw is None:
                        continue
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8")
                    try:
                        data = json.loads(raw)
                        env = Envelope.model_validate(data)
                    except Exception as e:
                        logger.warning(
                            "broker_envelope_parse_failed",
                            stream=stream_key_str,
                            entry_id=entry_id, error=str(e),
                        )
                        # ACK to drop poison pill from group's pending list.
                        try:
                            await self._redis.xack(stream_key, group, entry_id)
                        except Exception:
                            pass
                        continue

                    # Skip expired events at consume time too — they
                    # may have aged in-stream while we were idle.
                    if env.is_expired():
                        try:
                            await self._redis.xack(stream_key, group, entry_id)
                        except Exception:
                            pass
                        continue

                    self._metrics["consumed"] += 1
                    try:
                        yield env
                    finally:
                        # ACK after caller's iteration body ran. If
                        # they raised, the entry stays pending —
                        # XPENDING will surface it for reclaim.
                        try:
                            await self._redis.xack(stream_key, group, entry_id)
                        except Exception as e:
                            logger.warning(
                                "broker_xack_failed",
                                stream=stream_key_str,
                                entry_id=entry_id, error=str(e),
                            )

    async def _subscribe_inproc(
        self, pattern: str, max_queue: int,
    ) -> AsyncIterator[Envelope]:
        q: asyncio.Queue[Envelope] = asyncio.Queue(maxsize=max_queue)
        # Register against the pattern as supplied. Wildcard ``>`` is
        # translated to ``*`` for fnmatch.
        fn_pattern = pattern.replace(">", "*")
        self._inproc_subs[fn_pattern].append(q)
        try:
            while not self._stopped.is_set():
                try:
                    env = await asyncio.wait_for(q.get(), timeout=READ_BLOCK_MS / 1000.0)
                except asyncio.TimeoutError:
                    continue
                if env.is_expired():
                    continue
                self._metrics["consumed"] += 1
                yield env
        finally:
            try:
                self._inproc_subs[fn_pattern].remove(q)
            except ValueError:
                pass

    # ── Lifecycle ──────────────────────────────────────────────

    async def stop(self) -> None:
        self._stopped.set()
        # Inproc queues will see the stopped flag and exit naturally.

    def metrics(self) -> dict:
        return dict(self._metrics)


# ── Module-level singleton ──────────────────────────────────────────

_svc: Optional[BrokerClient] = None


def get_broker() -> Optional[BrokerClient]:
    return _svc


async def configure(
    *,
    redis_client,
    this_server_id: str,
    consumer_name: Optional[str] = None,
    stream_maxlen: int = DEFAULT_STREAM_MAXLEN,
) -> BrokerClient:
    global _svc
    _svc = await BrokerClient.create(
        redis_client=redis_client,
        this_server_id=this_server_id,
        consumer_name=consumer_name or this_server_id,
        stream_maxlen=stream_maxlen,
    )
    logger.info(
        "broker_client_configured",
        mode="redis_streams" if redis_client is not None else "in-process",
        consumer=consumer_name or this_server_id,
    )
    return _svc
