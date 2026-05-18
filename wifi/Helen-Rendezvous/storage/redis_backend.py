"""
Redis-backed storage backend for Helen-Rendezvous.

Supports three deployment topologies:
    * standalone   — single redis URL  (redis:// or rediss://)
    * sentinel     — list of (host, port) plus master name for HA
    * cluster      — list of nodes for Redis Cluster sharded deployments

All three return a `redis.asyncio.Redis`-compatible client. Public surface
(register_tunnel, lookup_tunnel, …) is identical to MemoryBackend so the
rest of the rendezvous swaps in by env var.

Key schema
----------
    tunnel:<peer_id>       JSON  EX ttl                tunnel registration
    signal:<key>           JSON  EX ttl                hole-punch endpoint
    lock:<key>             token EX ttl  NX            distributed lock
    rendezvous:events      pub/sub channel             cross-instance bus

Resilience
----------
* `redis.asyncio.ConnectionPool` with reconnect on the standalone path.
* Sentinel / cluster delegates failover to redis-py.
* Every operation is wrapped in a small retry-with-backoff helper that gives
  up after `OP_MAX_ATTEMPTS`. On give-up the call returns a sentinel (False /
  None / empty list) and bumps the `degraded` counter so /health reports
  trouble.
* `health()` records the last successful PING latency; if PING fails the
  status flips to `degraded` (some operations still succeed) or `down`
  (PINGs have been failing for > DEGRADE_AFTER_SEC).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
import time
from typing import Any, AsyncIterator, Optional

import structlog

logger = structlog.get_logger(__name__)


try:
    import redis.asyncio as _redis_async
    from redis.asyncio.cluster import RedisCluster as _RedisCluster
    from redis.asyncio.sentinel import Sentinel as _Sentinel
    from redis.exceptions import (
        ConnectionError as _RedisConnError,
        RedisError as _RedisError,
        TimeoutError as _RedisTimeoutError,
    )

    _REDIS_AVAILABLE = True
except ImportError:  # pragma: no cover
    _redis_async = None  # type: ignore[assignment]
    _RedisCluster = None  # type: ignore[assignment]
    _Sentinel = None  # type: ignore[assignment]
    _RedisError = Exception  # type: ignore[misc,assignment]
    _RedisConnError = Exception  # type: ignore[misc,assignment]
    _RedisTimeoutError = Exception  # type: ignore[misc,assignment]
    _REDIS_AVAILABLE = False


# Tunable knobs — operator may override via env (read by factory.py).
OP_MAX_ATTEMPTS = 4
OP_BACKOFF_BASE = 0.15
OP_BACKOFF_MAX = 2.0
DEGRADE_AFTER_SEC = 15.0
EVENTS_CHANNEL_DEFAULT = "rendezvous:events"


class RedisUnavailable(RuntimeError):
    """Raised when the operator selected a Redis backend but redis-py is missing."""


class RedisBackend:
    """Redis implementation of `StorageBackend`."""

    backend_name = "redis"

    def __init__(
        self,
        client: Any,
        *,
        mode: str = "standalone",
        events_channel: str = EVENTS_CHANNEL_DEFAULT,
        key_prefix: str = "",
    ) -> None:
        if not _REDIS_AVAILABLE:
            raise RedisUnavailable(
                "redis package not installed — `pip install redis[hiredis]>=5.0`"
            )
        self._client = client
        self._mode = mode
        self._events_channel = events_channel
        self._prefix = key_prefix
        self._closed = False
        self._last_ok_ping = 0.0
        self._last_error: Optional[str] = None
        self._consecutive_failures = 0
        self._op_count = 0

    # ── Factories ──────────────────────────────────────────

    @classmethod
    def from_url(
        cls,
        url: str,
        *,
        events_channel: str = EVENTS_CHANNEL_DEFAULT,
        key_prefix: str = "",
        max_connections: int = 64,
        socket_timeout: float = 5.0,
        socket_connect_timeout: float = 5.0,
        health_check_interval: int = 30,
        password: Optional[str] = None,
        username: Optional[str] = None,
        ssl_cert_reqs: Optional[str] = None,
    ) -> "RedisBackend":
        if not _REDIS_AVAILABLE:
            raise RedisUnavailable(
                "redis package not installed — `pip install redis[hiredis]>=5.0`"
            )
        kwargs: dict[str, Any] = dict(
            decode_responses=True,
            max_connections=max_connections,
            socket_timeout=socket_timeout,
            socket_connect_timeout=socket_connect_timeout,
            health_check_interval=health_check_interval,
            retry_on_timeout=True,
        )
        if password:
            kwargs["password"] = password
        if username:
            kwargs["username"] = username
        if ssl_cert_reqs and url.startswith("rediss://"):
            kwargs["ssl_cert_reqs"] = ssl_cert_reqs
        client = _redis_async.from_url(url, **kwargs)  # type: ignore[union-attr]
        logger.info("redis_backend_initialised", mode="standalone", url=_safe_url(url))
        return cls(
            client,
            mode="standalone",
            events_channel=events_channel,
            key_prefix=key_prefix,
        )

    @classmethod
    def from_sentinels(
        cls,
        sentinels: list[tuple[str, int]],
        master_name: str,
        *,
        password: Optional[str] = None,
        sentinel_password: Optional[str] = None,
        events_channel: str = EVENTS_CHANNEL_DEFAULT,
        key_prefix: str = "",
        socket_timeout: float = 5.0,
        ssl: bool = False,
    ) -> "RedisBackend":
        if not _REDIS_AVAILABLE:
            raise RedisUnavailable("redis package not installed")
        sentinel_kwargs: dict[str, Any] = {
            "socket_timeout": socket_timeout,
            "decode_responses": True,
        }
        if sentinel_password:
            sentinel_kwargs["password"] = sentinel_password
        if ssl:
            sentinel_kwargs["ssl"] = True
        sentinel = _Sentinel(sentinels, sentinel_kwargs=sentinel_kwargs)  # type: ignore[misc]
        master_kwargs: dict[str, Any] = {"socket_timeout": socket_timeout}
        if password:
            master_kwargs["password"] = password
        if ssl:
            master_kwargs["ssl"] = True
        master = sentinel.master_for(master_name, **master_kwargs)
        logger.info(
            "redis_backend_initialised",
            mode="sentinel",
            master=master_name,
            sentinels=len(sentinels),
        )
        return cls(
            master,
            mode="sentinel",
            events_channel=events_channel,
            key_prefix=key_prefix,
        )

    @classmethod
    def from_cluster(
        cls,
        nodes: list[tuple[str, int]],
        *,
        password: Optional[str] = None,
        username: Optional[str] = None,
        events_channel: str = EVENTS_CHANNEL_DEFAULT,
        key_prefix: str = "",
        ssl: bool = False,
    ) -> "RedisBackend":
        if not _REDIS_AVAILABLE:
            raise RedisUnavailable("redis package not installed")
        startup_nodes = [
            _redis_async.cluster.ClusterNode(host=h, port=p) for h, p in nodes  # type: ignore[union-attr]
        ]
        kwargs: dict[str, Any] = dict(
            decode_responses=True,
            startup_nodes=startup_nodes,
            ssl=ssl,
        )
        if password:
            kwargs["password"] = password
        if username:
            kwargs["username"] = username
        client = _RedisCluster(**kwargs)  # type: ignore[misc]
        logger.info("redis_backend_initialised", mode="cluster", nodes=len(nodes))
        return cls(
            client,
            mode="cluster",
            events_channel=events_channel,
            key_prefix=key_prefix,
        )

    # ── Internal: keying + retries ─────────────────────────

    def _k(self, ns: str, key: str) -> str:
        return f"{self._prefix}{ns}:{key}"

    async def _run(self, op_name: str, fn: Any, *args: Any, **kw: Any) -> Any:
        attempt = 0
        while True:
            try:
                self._op_count += 1
                result = await fn(*args, **kw)
                self._consecutive_failures = 0
                return result
            except (_RedisConnError, _RedisTimeoutError) as exc:
                attempt += 1
                self._consecutive_failures += 1
                self._last_error = f"{type(exc).__name__}: {exc}"
                if attempt >= OP_MAX_ATTEMPTS:
                    logger.warning(
                        "redis_op_giveup",
                        op=op_name,
                        attempts=attempt,
                        error=self._last_error,
                    )
                    raise
                delay = min(OP_BACKOFF_MAX, OP_BACKOFF_BASE * (2 ** (attempt - 1)))
                logger.info(
                    "redis_op_retry",
                    op=op_name,
                    attempt=attempt,
                    backoff_sec=delay,
                )
                await asyncio.sleep(delay)
            except _RedisError as exc:
                self._consecutive_failures += 1
                self._last_error = f"{type(exc).__name__}: {exc}"
                logger.error("redis_op_failed", op=op_name, error=self._last_error)
                raise

    # ── Tunnels ────────────────────────────────────────────

    async def register_tunnel(
        self,
        peer_id: str,
        info: dict[str, Any],
        ttl: int,
    ) -> str:
        key = self._k("tunnel", peer_id)
        body = json.dumps({**info, "peer_id": peer_id}, default=str)
        try:
            await self._run("SET tunnel", self._client.set, key, body, ex=max(1, ttl))
        except _RedisError:
            return ""
        return key

    async def lookup_tunnel(self, peer_id: str) -> Optional[dict[str, Any]]:
        key = self._k("tunnel", peer_id)
        try:
            raw = await self._run("GET tunnel", self._client.get, key)
        except _RedisError:
            return None
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("tunnel_json_decode_failed", key=key)
            return None

    async def unregister_tunnel(self, peer_id: str) -> bool:
        key = self._k("tunnel", peer_id)
        try:
            n = await self._run("DEL tunnel", self._client.delete, key)
        except _RedisError:
            return False
        return bool(n)

    async def list_tunnels(self) -> list[dict[str, Any]]:
        pattern = self._k("tunnel", "*")
        out: list[dict[str, Any]] = []
        try:
            async for k in self._scan(pattern):
                raw = await self._run("MGET tunnel", self._client.get, k)
                if raw is None:
                    continue
                try:
                    out.append(json.loads(raw))
                except json.JSONDecodeError:
                    continue
        except _RedisError:
            pass
        return out

    async def refresh_tunnel(self, peer_id: str, ttl: int) -> bool:
        key = self._k("tunnel", peer_id)
        try:
            return bool(await self._run("EXPIRE tunnel", self._client.expire, key, ttl))
        except _RedisError:
            return False

    # ── Signaling ──────────────────────────────────────────

    async def register_signal(
        self,
        key: str,
        payload: dict[str, Any],
        ttl: int,
    ) -> bool:
        full = self._k("signal", key)
        body = json.dumps(payload, default=str)
        try:
            await self._run("SET signal", self._client.set, full, body, ex=max(1, ttl))
            return True
        except _RedisError:
            return False

    async def lookup_signal(self, key: str) -> Optional[dict[str, Any]]:
        full = self._k("signal", key)
        try:
            raw = await self._run("GET signal", self._client.get, full)
        except _RedisError:
            return None
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    async def delete_signal(self, key: str) -> bool:
        full = self._k("signal", key)
        try:
            return bool(await self._run("DEL signal", self._client.delete, full))
        except _RedisError:
            return False

    async def list_signals(self) -> list[str]:
        pattern = self._k("signal", "*")
        out: list[str] = []
        prefix = self._k("signal", "")
        try:
            async for k in self._scan(pattern):
                # strip "signal:" prefix
                if k.startswith(prefix):
                    out.append(k[len(prefix):])
                else:
                    out.append(k)
        except _RedisError:
            pass
        return out

    async def _scan(self, pattern: str, batch: int = 200) -> AsyncIterator[str]:
        # Cluster mode requires per-shard scans — redis-py handles that when
        # we call scan_iter on a RedisCluster client.
        async for key in self._client.scan_iter(match=pattern, count=batch):
            yield key

    # ── Pub/sub ────────────────────────────────────────────

    async def publish_event(self, channel: str, payload: dict[str, Any]) -> int:
        try:
            n = await self._run(
                "PUBLISH",
                self._client.publish,
                channel,
                json.dumps(payload, default=str),
            )
            return int(n or 0)
        except _RedisError:
            return 0

    async def subscribe_events(  # type: ignore[override]
        self,
        channel: str,
    ) -> AsyncIterator[dict[str, Any]]:
        pubsub = self._client.pubsub()
        await pubsub.subscribe(channel)
        try:
            async for msg in pubsub.listen():
                if msg is None:
                    continue
                if msg.get("type") not in ("message", "pmessage"):
                    continue
                data = msg.get("data")
                if data is None:
                    continue
                if isinstance(data, bytes):
                    try:
                        data = data.decode("utf-8")
                    except UnicodeDecodeError:
                        continue
                try:
                    yield json.loads(data)
                except (TypeError, json.JSONDecodeError):
                    continue
        finally:
            with contextlib.suppress(Exception):
                await pubsub.unsubscribe(channel)
            with contextlib.suppress(Exception):
                await pubsub.close()

    # ── Locks ──────────────────────────────────────────────

    async def acquire_lock(self, key: str, ttl: int) -> Optional[str]:
        full = self._k("lock", key)
        token = secrets.token_hex(16)
        try:
            ok = await self._run(
                "SET NX EX lock",
                self._client.set,
                full,
                token,
                nx=True,
                ex=max(1, ttl),
            )
        except _RedisError:
            return None
        return token if ok else None

    async def release_lock(self, key: str, token: str) -> bool:
        full = self._k("lock", key)
        lua = """
        if redis.call('GET', KEYS[1]) == ARGV[1] then
            return redis.call('DEL', KEYS[1])
        else
            return 0
        end
        """
        try:
            n = await self._run("EVAL release_lock", self._client.eval, lua, 1, full, token)
        except _RedisError:
            return False
        return bool(n)

    # ── Health ─────────────────────────────────────────────

    async def health(self) -> dict[str, Any]:
        t0 = time.monotonic()
        try:
            await asyncio.wait_for(self._client.ping(), timeout=2.0)
            latency_ms = (time.monotonic() - t0) * 1000.0
            self._last_ok_ping = time.time()
            self._consecutive_failures = 0
            status = "ok"
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
            self._consecutive_failures += 1
            age = (
                time.time() - self._last_ok_ping
                if self._last_ok_ping
                else DEGRADE_AFTER_SEC + 1
            )
            status = "down" if age > DEGRADE_AFTER_SEC else "degraded"
            latency_ms = -1.0
        return {
            "backend": "redis",
            "status": status,
            "latency_ms": round(latency_ms, 3),
            "details": {
                "mode": self._mode,
                "ops_total": self._op_count,
                "consecutive_failures": self._consecutive_failures,
                "last_error": self._last_error,
                "events_channel": self._events_channel,
            },
        }

    # ── Lifecycle ──────────────────────────────────────────

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(Exception):
            await self._client.aclose()
        logger.info("redis_backend_closed", mode=self._mode)


def _safe_url(url: str) -> str:
    """Strip credentials from a redis URL so it can be logged safely."""
    if "@" not in url:
        return url
    scheme, rest = url.split("://", 1)
    _, host = rest.split("@", 1)
    return f"{scheme}://***@{host}"
