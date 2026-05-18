"""
Distributed lock service — Redis SETNX with token-based ownership.

Replaces the in-memory ``threading.RLock`` used in places like
``call_signal_authz`` for cross-server safety. Without this, two
servers can both believe they own a call's authoritative state and
race on writes.

API
---
    >>> lock = DistributedLockService(redis_client)
    >>> token = await lock.acquire("call:lease:abc", ttl_seconds=30)
    >>> if token:
    ...     try:
    ...         # critical section
    ...         await lock.extend("call:lease:abc", token, ttl_seconds=30)
    ...     finally:
    ...         await lock.release("call:lease:abc", token)

Or as an async context manager::

    >>> async with lock.acquire_ctx("call:lease:abc", ttl=30) as token:
    ...     if token:
    ...         do_work()

Design constraints
------------------
* **Token-based release** — only the owner can release. Prevents a
  caller whose lease expired from accidentally releasing a lock the
  next owner just acquired.
* **Lua script for release/extend** — atomic compare-then-act so we
  don't have a TOCTOU window.
* **Auto-renew helper** — long-held leases (call origin election)
  should renew at half the TTL. ``hold(...)`` runs the renewal loop in
  a background task and yields cancellation when the caller exits.
* **Fallback to in-process when Redis is unavailable** — degrades to
  asyncio.Lock keyed by name. Useful for dev / single-server LAN. The
  ``is_distributed`` property tells callers whether the lock is
  globally authoritative.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import secrets
from typing import Optional, AsyncIterator

from app.core.logging import get_logger

logger = get_logger(__name__)

# Atomic release: only delete the key if its value matches our token.
# Prevents a leaked lease from un-locking a lock that has already been
# re-acquired by another caller.
_LUA_RELEASE = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("DEL", KEYS[1])
else
    return 0
end
"""

# Atomic extend: only update TTL if we still own the lock. Same TOCTOU
# concern as release.
_LUA_EXTEND = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("PEXPIRE", KEYS[1], ARGV[2])
else
    return 0
end
"""


class DistributedLockService:
    """Redis-backed distributed lock with auto-renewal helpers.

    Pass a ``redis.asyncio.Redis`` client at construction. If ``None``
    is passed, the service falls back to in-process ``asyncio.Lock``
    instances keyed by lock name — useful for dev/single-process
    deployments. The ``is_distributed`` property indicates which mode
    the service is operating in.
    """

    def __init__(self, redis_client=None) -> None:
        self._redis = redis_client
        # In-process fallback locks. Keyed by name. Created on demand.
        self._local_locks: dict[str, asyncio.Lock] = {}
        self._local_owners: dict[str, str] = {}
        self._local_lock = asyncio.Lock()

    @property
    def is_distributed(self) -> bool:
        return self._redis is not None

    async def acquire(
        self, name: str, ttl_seconds: int = 30,
    ) -> Optional[str]:
        """Try to acquire ``name`` for ``ttl_seconds``. Returns an
        opaque token on success (caller must pass it back to
        ``extend()`` and ``release()``), or ``None`` on contention.
        Does NOT block — callers should retry with backoff if they
        want blocking semantics."""
        token = secrets.token_urlsafe(16)
        if self._redis is not None:
            # SET NX PX — atomic acquire with millisecond TTL.
            ok = await self._redis.set(
                f"helen:lock:{name}",
                token,
                nx=True,
                px=int(ttl_seconds * 1000),
            )
            return token if ok else None

        # In-process fallback.
        async with self._local_lock:
            if name in self._local_owners:
                return None
            self._local_owners[name] = token
            return token

    async def release(self, name: str, token: str) -> bool:
        """Release ``name`` if (and only if) we still own it. Returns
        True on a successful release, False if our lease had expired
        and another holder has taken over. Idempotent."""
        if self._redis is not None:
            try:
                released = await self._redis.eval(
                    _LUA_RELEASE, 1, f"helen:lock:{name}", token,
                )
                return bool(released)
            except Exception as e:
                logger.warning("lock_release_failed", name=name, error=str(e))
                return False

        async with self._local_lock:
            if self._local_owners.get(name) == token:
                self._local_owners.pop(name, None)
                return True
            return False

    async def extend(
        self, name: str, token: str, ttl_seconds: int,
    ) -> bool:
        """Extend our lease on ``name`` by ``ttl_seconds`` if we still
        own it. Returns True on successful extension, False if the
        lease has expired or transferred."""
        if self._redis is not None:
            try:
                ok = await self._redis.eval(
                    _LUA_EXTEND, 1,
                    f"helen:lock:{name}", token, str(int(ttl_seconds * 1000)),
                )
                return bool(ok)
            except Exception as e:
                logger.warning("lock_extend_failed", name=name, error=str(e))
                return False

        # In-process: ownership doesn't expire on its own, so extend
        # is a no-op past the ownership check.
        async with self._local_lock:
            return self._local_owners.get(name) == token

    @contextlib.asynccontextmanager
    async def acquire_ctx(
        self, name: str, ttl_seconds: int = 30,
    ) -> AsyncIterator[Optional[str]]:
        """Async context manager. Yields the token on acquire, ``None``
        on contention. Always releases on exit."""
        token = await self.acquire(name, ttl_seconds)
        try:
            yield token
        finally:
            if token is not None:
                await self.release(name, token)

    async def hold(
        self,
        name: str,
        ttl_seconds: int = 30,
        renew_every: Optional[float] = None,
    ) -> "_HeldLock":
        """Acquire ``name`` and start a background renewal task that
        extends the lease every ``renew_every`` seconds (defaults to
        half TTL). Returns a context-manager-like ``_HeldLock`` object
        whose ``.token`` is the acquired token (or ``None`` on
        contention). Caller MUST call ``.release()`` when done — or
        use ``async with`` over the result."""
        token = await self.acquire(name, ttl_seconds)
        return _HeldLock(self, name, token, ttl_seconds, renew_every)


class _HeldLock:
    """Represents an acquired distributed lock with a background
    renewal loop. Use ``async with`` for guaranteed cleanup."""

    def __init__(
        self,
        svc: DistributedLockService,
        name: str,
        token: Optional[str],
        ttl_seconds: int,
        renew_every: Optional[float] = None,
    ):
        self._svc = svc
        self._name = name
        self._ttl = ttl_seconds
        self._renew_every = renew_every if renew_every is not None else ttl_seconds / 2
        self.token = token
        self._stop_event = asyncio.Event()
        self._renewal_task: Optional[asyncio.Task] = None

        if token is not None:
            self._renewal_task = asyncio.create_task(self._renew_loop())

    @property
    def acquired(self) -> bool:
        return self.token is not None

    async def _renew_loop(self):
        try:
            while not self._stop_event.is_set():
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=self._renew_every,
                    )
                    return  # stop signaled
                except asyncio.TimeoutError:
                    pass  # renewal interval elapsed
                ok = await self._svc.extend(self._name, self.token, self._ttl)
                if not ok:
                    logger.warning(
                        "distributed_lock_lease_lost",
                        name=self._name,
                    )
                    self.token = None  # we lost it; flag to caller
                    return
        except asyncio.CancelledError:
            return

    async def release(self):
        self._stop_event.set()
        if self._renewal_task:
            self._renewal_task.cancel()
            try:
                await self._renewal_task
            except (asyncio.CancelledError, BaseException):
                pass
        if self.token:
            await self._svc.release(self._name, self.token)
            self.token = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.release()


# ── Module-level singleton ──────────────────────────────────────────
# Late-bound so app/main.py can supply the redis client at startup.

_svc: Optional[DistributedLockService] = None


def get_lock_service() -> DistributedLockService:
    global _svc
    if _svc is None:
        # Default to in-process. app/main.py replaces with Redis-backed
        # at startup once redis_client is constructed.
        _svc = DistributedLockService(redis_client=None)
    return _svc


def configure(redis_client) -> DistributedLockService:
    """Install a Redis-backed lock service as the module singleton.
    Call from app/main.py after the redis client is connected."""
    global _svc
    _svc = DistributedLockService(redis_client=redis_client)
    logger.info(
        "distributed_lock_service_configured",
        mode="redis" if redis_client is not None else "in-process",
    )
    return _svc
