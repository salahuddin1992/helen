"""
Leader election for long-running singleton background tasks.

Motivation
----------
Several background loops MUST run on exactly one worker when the server is
scaled horizontally:

* :class:`DLQReaper` — reprocesses the messaging dead-letter queue.
* :class:`ScheduledMessageService` — dispatches due scheduled messages.
* :class:`WebhookService` — dispatches pending outbound webhooks.
* ``GroupFileService.sweep_expired`` / ``cleanup_stale_recipients`` — GC
  loops that mutate global state.

Running them on every worker is either wasteful (sweeper fan-out) or
catastrophic (scheduled messages fire N times). This module provides a
pluggable :class:`LeaderElection` abstraction with three backends:

* **Single-process** (default, SQLite / dev): always leader; zero I/O.
* **PostgreSQL advisory lock** (``pg_try_advisory_lock``): transactional,
  released automatically if the owning session dies.
* **Redis SET NX PX heartbeat**: external Redis with periodic heartbeat;
  a failed heartbeat hands off leadership within ``ttl_seconds``.

Resolution order is controlled by
:func:`app.core.config.Settings.leader_election_backend`
(``single`` | ``postgres`` | ``redis``). When unset we infer from
``DB_BACKEND``: ``postgresql`` → ``postgres``, otherwise ``single``.

Usage
-----

```python
from app.services.leader_election import run_as_leader

async def my_sweep():
    while True:
        if await leader.try_acquire():
            do_work()
        await asyncio.sleep(interval)
```

Or the higher-level helper:

```python
await run_as_leader("group_file_sweeper", interval=120, fn=_run_one_pass)
```

``run_as_leader`` owns the loop, the heartbeat refresh, and the
stop-signal plumbing; callers supply only the one-pass coroutine.
"""

from __future__ import annotations

import asyncio
import os
import socket
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.session import engine

logger = get_logger(__name__)
_settings = get_settings()


# ─────────────────────────────────────────────────────────────────────────────
# Worker identity
# ─────────────────────────────────────────────────────────────────────────────

def _worker_identity() -> str:
    """Stable identity for this Python process — used as the Redis lock value
    and the advisory-lock "claimant" record.
    """
    host = socket.gethostname()
    pid = os.getpid()
    nonce = uuid.uuid4().hex[:8]
    return f"{host}:{pid}:{nonce}"


# ─────────────────────────────────────────────────────────────────────────────
# Lock key hashing (Postgres advisory locks use 64-bit BIGINT keys)
# ─────────────────────────────────────────────────────────────────────────────

def _lock_key(name: str) -> int:
    """
    Deterministic 64-bit int for Postgres advisory locks.

    Hash-collision odds: negligible at the dozens-of-keys scale we use.
    """
    import hashlib
    digest = hashlib.blake2b(name.encode("utf-8"), digest_size=8).digest()
    value = int.from_bytes(digest, "big", signed=True)
    # pg_advisory_lock takes a signed bigint; collapse to signed range.
    if value > 0x7FFF_FFFF_FFFF_FFFF:
        value -= 0x1_0000_0000_0000_0000
    return value


# ─────────────────────────────────────────────────────────────────────────────
# Backend interface
# ─────────────────────────────────────────────────────────────────────────────

class LeaderElectionBackend(ABC):
    """Backend contract — all methods must be idempotent and safe to call
    repeatedly while the loop is running."""

    @abstractmethod
    async def try_acquire(self, name: str, ttl_seconds: int) -> bool:
        """Attempt to become (or remain) leader for ``name``. Returns True
        when this worker currently holds the lock."""

    @abstractmethod
    async def heartbeat(self, name: str, ttl_seconds: int) -> bool:
        """Refresh the lease. Returns True on success, False if the lock
        was lost (e.g. we were preempted)."""

    @abstractmethod
    async def release(self, name: str) -> None:
        """Best-effort release. Swallows errors — shutdown paths shouldn't
        raise."""


# ─────────────────────────────────────────────────────────────────────────────
# Single-process backend — always leader, no coordination
# ─────────────────────────────────────────────────────────────────────────────

class _SingleProcessBackend(LeaderElectionBackend):
    """Always returns True — safe for SQLite / single-worker deployments."""

    async def try_acquire(self, name: str, ttl_seconds: int) -> bool:
        return True

    async def heartbeat(self, name: str, ttl_seconds: int) -> bool:
        return True

    async def release(self, name: str) -> None:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Postgres advisory-lock backend
# ─────────────────────────────────────────────────────────────────────────────

class _PostgresAdvisoryBackend(LeaderElectionBackend):
    """Session-scoped ``pg_try_advisory_lock`` — lock is released when the
    underlying session disconnects, so heartbeats just re-assert.
    """

    def __init__(self, pg_engine: AsyncEngine, identity: str) -> None:
        self._engine = pg_engine
        self._identity = identity
        # One dedicated connection per held lock — advisory locks are
        # bound to the session that acquired them.
        self._conns: dict[str, object] = {}

    async def try_acquire(self, name: str, ttl_seconds: int) -> bool:
        if name in self._conns:
            # Already leader — liveness check.
            return await self.heartbeat(name, ttl_seconds)
        key = _lock_key(name)
        conn = await self._engine.connect()
        try:
            res = await conn.execute(
                text("SELECT pg_try_advisory_lock(:k)"), {"k": key},
            )
            locked = bool(res.scalar())
        except Exception as e:
            await conn.close()
            logger.warning("pg_advisory_lock_error", name=name, error=str(e))
            return False
        if not locked:
            await conn.close()
            return False
        self._conns[name] = conn
        logger.info("leader_acquired", name=name, identity=self._identity,
                    backend="postgres")
        return True

    async def heartbeat(self, name: str, ttl_seconds: int) -> bool:
        conn = self._conns.get(name)
        if conn is None:
            return False
        try:
            # SELECT 1 — confirms the connection is still alive; the
            # advisory lock is automatic while the session lives.
            await conn.execute(text("SELECT 1"))
            return True
        except Exception as e:
            logger.warning("pg_advisory_heartbeat_failed", name=name,
                           error=str(e))
            try:
                await conn.close()
            except Exception:
                pass
            self._conns.pop(name, None)
            return False

    async def release(self, name: str) -> None:
        conn = self._conns.pop(name, None)
        if conn is None:
            return
        try:
            await conn.execute(
                text("SELECT pg_advisory_unlock(:k)"),
                {"k": _lock_key(name)},
            )
        except Exception:
            pass
        try:
            await conn.close()
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Redis backend — SET NX PX with periodic refresh + Lua release
# ─────────────────────────────────────────────────────────────────────────────

class _RedisBackend(LeaderElectionBackend):
    """Redis lease lock.

    SET key owner NX PX ttl_ms
      → atomic create-if-absent with expiry
    Lua release script
      → only deletes the key if the stored value still matches our owner,
        so a lost-then-reacquired lease can't be released by the previous
        owner.
    """

    _RELEASE_SCRIPT = """
    if redis.call('GET', KEYS[1]) == ARGV[1] then
      return redis.call('DEL', KEYS[1])
    else
      return 0
    end
    """

    _REFRESH_SCRIPT = """
    if redis.call('GET', KEYS[1]) == ARGV[1] then
      return redis.call('PEXPIRE', KEYS[1], ARGV[2])
    else
      return 0
    end
    """

    def __init__(self, url: str, identity: str) -> None:
        import redis.asyncio as redis_async  # type: ignore
        self._redis = redis_async.from_url(url, decode_responses=True)
        self._identity = identity

    def _key(self, name: str) -> str:
        return f"commclient:leader:{name}"

    async def try_acquire(self, name: str, ttl_seconds: int) -> bool:
        try:
            ok = await self._redis.set(
                self._key(name),
                self._identity,
                nx=True,
                px=ttl_seconds * 1000,
            )
            if ok:
                logger.info("leader_acquired", name=name,
                            identity=self._identity, backend="redis")
                return True
            # Already held — check if it's us (refresh path).
            current = await self._redis.get(self._key(name))
            if current == self._identity:
                return await self.heartbeat(name, ttl_seconds)
            return False
        except Exception as e:
            logger.warning("redis_acquire_error", name=name, error=str(e))
            return False

    async def heartbeat(self, name: str, ttl_seconds: int) -> bool:
        try:
            res = await self._redis.eval(
                self._REFRESH_SCRIPT,
                1,
                self._key(name),
                self._identity,
                str(ttl_seconds * 1000),
            )
            return bool(res)
        except Exception as e:
            logger.warning("redis_heartbeat_error", name=name, error=str(e))
            return False

    async def release(self, name: str) -> None:
        try:
            await self._redis.eval(
                self._RELEASE_SCRIPT, 1, self._key(name), self._identity,
            )
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Singleton election handle
# ─────────────────────────────────────────────────────────────────────────────

_identity = _worker_identity()


def _build_backend() -> LeaderElectionBackend:
    """Select the backend based on settings. Fails safe to single-process."""
    backend = (
        getattr(_settings, "LEADER_ELECTION_BACKEND", None)
        or _auto_detect_backend()
    ).lower()

    if backend == "single":
        return _SingleProcessBackend()

    if backend == "postgres":
        try:
            return _PostgresAdvisoryBackend(engine, _identity)
        except Exception as e:
            logger.error("leader_pg_backend_init_failed", error=str(e))
            return _SingleProcessBackend()

    if backend == "redis":
        url = getattr(_settings, "REDIS_URL", None) or os.environ.get(
            "REDIS_URL"
        )
        if not url:
            logger.warning("leader_redis_no_url_fallback_single")
            return _SingleProcessBackend()
        try:
            return _RedisBackend(url, _identity)
        except Exception as e:
            logger.error("leader_redis_backend_init_failed", error=str(e))
            return _SingleProcessBackend()

    logger.warning("leader_unknown_backend_fallback_single", backend=backend)
    return _SingleProcessBackend()


def _auto_detect_backend() -> str:
    """Fallback when LEADER_ELECTION_BACKEND is not set explicitly."""
    db_backend = getattr(_settings, "DB_BACKEND", "sqlite")
    if db_backend == "postgresql":
        return "postgres"
    return "single"


_backend: LeaderElectionBackend = _build_backend()


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_LEASE_TTL = 60  # seconds


async def try_acquire(name: str, ttl_seconds: int = DEFAULT_LEASE_TTL) -> bool:
    """Attempt to become leader for ``name``."""
    return await _backend.try_acquire(name, ttl_seconds)


async def heartbeat(name: str, ttl_seconds: int = DEFAULT_LEASE_TTL) -> bool:
    """Refresh leadership. Returns False if the lease was lost."""
    return await _backend.heartbeat(name, ttl_seconds)


async def release(name: str) -> None:
    """Best-effort release — called on graceful shutdown."""
    await _backend.release(name)


@dataclass
class LeaderLoopConfig:
    name: str
    interval: float
    fn: Callable[[], Awaitable[None]]
    ttl_seconds: int = DEFAULT_LEASE_TTL
    initial_delay: float = 0.0
    jitter: float = 0.1
    on_lost: Optional[Callable[[str], Awaitable[None]]] = None


async def run_as_leader(cfg: LeaderLoopConfig) -> None:
    """
    Run ``cfg.fn`` every ``cfg.interval`` seconds iff this worker is the
    current leader for ``cfg.name``.

    Lease-lost handling:
      * If a heartbeat fails, we stop invoking ``fn`` until
        ``try_acquire`` succeeds again.
      * ``on_lost`` is invoked (best-effort) when the lease flips away.

    The loop is intended to be scheduled via ``asyncio.create_task`` and
    cancelled on shutdown.
    """
    if cfg.initial_delay > 0:
        await asyncio.sleep(cfg.initial_delay)

    is_leader = False
    last_heartbeat = 0.0

    try:
        while True:
            try:
                if not is_leader:
                    is_leader = await try_acquire(cfg.name, cfg.ttl_seconds)
                    last_heartbeat = time.monotonic() if is_leader else 0.0
                else:
                    # Heartbeat a bit before lease expires.
                    if (time.monotonic() - last_heartbeat) > (
                        cfg.ttl_seconds * 0.5
                    ):
                        ok = await heartbeat(cfg.name, cfg.ttl_seconds)
                        if not ok:
                            logger.warning("leader_lease_lost",
                                           name=cfg.name)
                            is_leader = False
                            if cfg.on_lost:
                                try:
                                    await cfg.on_lost(cfg.name)
                                except Exception as e:
                                    logger.error(
                                        "leader_on_lost_handler_failed",
                                        name=cfg.name, error=str(e),
                                    )
                        else:
                            last_heartbeat = time.monotonic()

                if is_leader:
                    try:
                        await cfg.fn()
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        logger.error("leader_work_fn_failed",
                                     name=cfg.name, error=str(e))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("leader_loop_iteration_error",
                             name=cfg.name, error=str(e))

            sleep_for = cfg.interval
            # Small jitter so multiple loops don't all wake in lockstep.
            if cfg.jitter > 0:
                import random
                sleep_for *= 1 + random.uniform(-cfg.jitter, cfg.jitter)
            await asyncio.sleep(max(1.0, sleep_for))
    finally:
        if is_leader:
            try:
                await release(cfg.name)
            except Exception:
                pass


async def run_supervised_as_leader(
    name: str,
    factory: Callable[[], Awaitable[None]],
    *,
    ttl_seconds: int = DEFAULT_LEASE_TTL,
    check_interval: float = 5.0,
    initial_delay: float = 0.0,
) -> None:
    """
    Supervise an external long-running coroutine so it only runs while
    this worker holds leadership for ``name``.

    ``factory()`` MUST return a fresh awaitable each time it's called — we
    invoke it once per leadership acquisition, cancel the resulting task
    when the lease is lost, and recall ``factory()`` next time we become
    leader again.

    This is the adapter for services that own their own internal loops
    (``ScheduledMessageService.run_dispatch_loop``,
    ``WebhookService.run_dispatch_loop``,
    ``DeadLetterService._reaper_loop``), where pulling the loop body out
    into a one-shot pass would be invasive.

    Parameters
    ----------
    name           Leader-election lock name.
    factory        Zero-arg callable returning the long-running coroutine.
    ttl_seconds    Lease TTL.
    check_interval How often we poll leadership state (seconds).
    initial_delay  Delay before first acquisition attempt.
    """
    if initial_delay > 0:
        await asyncio.sleep(initial_delay)

    task: Optional[asyncio.Task] = None
    is_leader = False
    last_heartbeat = 0.0

    try:
        while True:
            try:
                if not is_leader:
                    if await try_acquire(name, ttl_seconds):
                        is_leader = True
                        last_heartbeat = time.monotonic()
                        logger.info(
                            "supervised_leader_started", name=name,
                            identity=_identity,
                        )
                        try:
                            task = asyncio.create_task(factory())
                        except Exception as e:
                            logger.error(
                                "supervised_factory_failed", name=name,
                                error=str(e),
                            )
                            # Release immediately so another worker may try.
                            await release(name)
                            is_leader = False
                else:
                    # Heartbeat refresh.
                    if (time.monotonic() - last_heartbeat) > (
                        ttl_seconds * 0.5
                    ):
                        ok = await heartbeat(name, ttl_seconds)
                        if not ok:
                            logger.warning(
                                "supervised_leader_lease_lost", name=name,
                            )
                            is_leader = False
                            if task and not task.done():
                                task.cancel()
                                try:
                                    await asyncio.wait_for(task, timeout=5.0)
                                except (
                                    asyncio.CancelledError,
                                    asyncio.TimeoutError,
                                    Exception,
                                ):
                                    pass
                            task = None
                        else:
                            last_heartbeat = time.monotonic()

                    # If the supervised task exited on its own while we
                    # remained leader, log + restart so transient errors
                    # inside the loop don't strand the service.
                    if is_leader and task is not None and task.done():
                        exc = task.exception() if not task.cancelled() else None
                        if exc is not None:
                            logger.error(
                                "supervised_task_crashed", name=name,
                                error=str(exc),
                            )
                        else:
                            logger.info(
                                "supervised_task_exited_restarting", name=name,
                            )
                        try:
                            task = asyncio.create_task(factory())
                        except Exception as e:
                            logger.error(
                                "supervised_factory_restart_failed",
                                name=name, error=str(e),
                            )
                            task = None
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(
                    "supervised_loop_iteration_error", name=name,
                    error=str(e),
                )

            await asyncio.sleep(check_interval)
    finally:
        if task is not None and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass
        if is_leader:
            try:
                await release(name)
            except Exception:
                pass


__all__ = [
    "DEFAULT_LEASE_TTL",
    "LeaderElectionBackend",
    "LeaderLoopConfig",
    "heartbeat",
    "release",
    "run_as_leader",
    "run_supervised_as_leader",
    "try_acquire",
]
