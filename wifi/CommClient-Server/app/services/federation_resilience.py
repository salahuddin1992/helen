"""
Federation resilience — Redis-backed origin cache + per-peer circuit breaker.

Audit fixes #2 and #3 from the cluster-readiness report:

  #2 ORIGIN CACHE → REDIS
     `federated_emit._origin_cache` was a process-local dict, so every
     fresh server process started with cold cache and had to flood every
     peer on first emit per user. Now backed by Redis hash
     `helen:fed:origin` when HELEN_REDIS_URL is set; the local dict
     becomes an L1 cache for hot reads. Failures degrade silently to
     local-only (no crashes).

  #3 CIRCUIT BREAKER
     `federation_router.forward_to_all_peers` would happily fan out to a
     dead peer on every emit, paying connect-timeout costs each time.
     Now each peer_id has a breaker: 3 consecutive failures opens it for
     30s; half-open allows one probe; success closes it.

Both helpers are async-safe (single-process locks) and import-time cheap
(no network I/O until first call).
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from typing import Optional

from app.core.logging import get_logger

logger = get_logger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Redis-backed origin cache
# ─────────────────────────────────────────────────────────────────────

_REDIS_URL = os.environ.get("HELEN_REDIS_URL", "").strip()
_REDIS_KEY_PREFIX = os.environ.get("HELEN_REDIS_PREFIX", "helen").strip() or "helen"
_ORIGIN_HASH = f"{_REDIS_KEY_PREFIX}:fed:origin"
_ORIGIN_TTL_SECONDS = float(os.environ.get("HELEN_FED_ORIGIN_TTL_SEC", "900"))

# L1 cache (in-process) — Redis is L2. user_id -> (server_id, expires_at)
_l1_cache: dict[str, tuple[str, float]] = {}
_l1_lock = asyncio.Lock()

_redis_client = None
_redis_init_lock = asyncio.Lock()
_redis_init_done = False


async def _get_redis():
    """Lazy redis.asyncio client. Returns None if Redis is unavailable."""
    global _redis_client, _redis_init_done
    if _redis_init_done:
        return _redis_client
    async with _redis_init_lock:
        if _redis_init_done:
            return _redis_client
        _redis_init_done = True
        if not _REDIS_URL:
            return None
        try:
            from redis.asyncio import from_url as _from_url
            client = _from_url(_REDIS_URL, decode_responses=True)
            # Quick PING so we fail fast if the URL is wrong.
            await client.ping()
            _redis_client = client
            logger.info("federation_redis_cache_enabled", key=_ORIGIN_HASH)
        except Exception as exc:
            logger.warning(
                "federation_redis_cache_unavailable",
                error=str(exc),
                note="origin cache will run in-process only — cold restarts will fan out",
            )
            _redis_client = None
        return _redis_client


async def remember_origin(user_id: str, server_id: str) -> None:
    """Persist user→server mapping to L1 + Redis."""
    if not user_id or not server_id:
        return
    expires = time.time() + _ORIGIN_TTL_SECONDS
    async with _l1_lock:
        _l1_cache[user_id] = (server_id, expires)
    client = await _get_redis()
    if client is None:
        return
    try:
        # Use a per-user key with EXPIRE rather than HSET so each user's
        # entry has its own TTL. Hash field TTLs aren't widely supported.
        key = f"{_ORIGIN_HASH}:{user_id}"
        await client.set(key, server_id, ex=int(_ORIGIN_TTL_SECONDS))
    except Exception as exc:
        logger.debug("federation_redis_set_fail", user_id=user_id[:12], error=str(exc))


async def forget_origin(user_id: str) -> None:
    """Drop the cached mapping (e.g. after a stale delivery failure)."""
    if not user_id:
        return
    async with _l1_lock:
        _l1_cache.pop(user_id, None)
    client = await _get_redis()
    if client is None:
        return
    try:
        await client.delete(f"{_ORIGIN_HASH}:{user_id}")
    except Exception as exc:
        logger.debug("federation_redis_del_fail", user_id=user_id[:12], error=str(exc))


async def lookup_origin(user_id: str) -> Optional[str]:
    """Return the cached server_id for `user_id`, or None.

    Order: L1 (fast) → Redis (cluster-shared). Stale L1 entries are evicted.
    """
    if not user_id:
        return None
    now = time.time()
    async with _l1_lock:
        entry = _l1_cache.get(user_id)
        if entry is not None:
            server_id, expires = entry
            if expires > now:
                return server_id
            _l1_cache.pop(user_id, None)
    client = await _get_redis()
    if client is None:
        return None
    try:
        val = await client.get(f"{_ORIGIN_HASH}:{user_id}")
    except Exception as exc:
        logger.debug("federation_redis_get_fail", user_id=user_id[:12], error=str(exc))
        return None
    if not val:
        return None
    # Promote into L1 so the next lookup is free.
    async with _l1_lock:
        _l1_cache[user_id] = (val, now + _ORIGIN_TTL_SECONDS)
    return val


# ─────────────────────────────────────────────────────────────────────
# Per-peer circuit breaker
# ─────────────────────────────────────────────────────────────────────

_BREAKER_FAILURE_THRESHOLD = int(os.environ.get("HELEN_FED_BREAKER_FAIL_THRESHOLD", "3"))
_BREAKER_OPEN_SECONDS = float(os.environ.get("HELEN_FED_BREAKER_OPEN_SEC", "30"))


@dataclass
class _BreakerState:
    failures: int = 0
    opened_at: float = 0.0  # 0 = closed
    half_open_in_flight: bool = False


_breakers: dict[str, _BreakerState] = {}
_breakers_lock = asyncio.Lock()


async def _get_state(peer_id: str) -> _BreakerState:
    async with _breakers_lock:
        st = _breakers.get(peer_id)
        if st is None:
            st = _BreakerState()
            _breakers[peer_id] = st
        return st


async def can_attempt(peer_id: str) -> bool:
    """Returns True if a request to this peer should proceed.

    State machine:
      closed:    failures < threshold → True
      open:      now < opened_at + cooldown → False
      half-open: cooldown elapsed → allow exactly one probe at a time
    """
    if not peer_id:
        return True
    st = await _get_state(peer_id)
    if st.opened_at == 0.0:
        return True  # closed
    now = time.time()
    if now < st.opened_at + _BREAKER_OPEN_SECONDS:
        return False  # open
    # cooldown elapsed — half-open: only one probe in flight
    async with _breakers_lock:
        if st.half_open_in_flight:
            return False
        st.half_open_in_flight = True
        return True


async def record_success(peer_id: str) -> None:
    """Reset the breaker — peer is healthy."""
    if not peer_id:
        return
    st = await _get_state(peer_id)
    async with _breakers_lock:
        if st.opened_at != 0.0 or st.failures > 0:
            logger.info("federation_breaker_closed", peer=peer_id[:12])
        st.failures = 0
        st.opened_at = 0.0
        st.half_open_in_flight = False


async def record_failure(peer_id: str) -> None:
    """Bump failure count and open the breaker if past threshold."""
    if not peer_id:
        return
    st = await _get_state(peer_id)
    async with _breakers_lock:
        st.failures += 1
        st.half_open_in_flight = False
        if st.opened_at == 0.0 and st.failures >= _BREAKER_FAILURE_THRESHOLD:
            st.opened_at = time.time()
            logger.warning(
                "federation_breaker_opened",
                peer=peer_id[:12],
                failures=st.failures,
                cooldown_sec=_BREAKER_OPEN_SECONDS,
            )


async def breaker_snapshot() -> dict[str, dict]:
    """Diagnostic — current state of every breaker."""
    async with _breakers_lock:
        return {
            pid: {
                "failures": st.failures,
                "open": st.opened_at != 0.0,
                "open_for_sec": (time.time() - st.opened_at) if st.opened_at else 0,
                "half_open_in_flight": st.half_open_in_flight,
            }
            for pid, st in _breakers.items()
        }


# ─────────────────────────────────────────────────────────────────────
# Retry helper for transient HTTP errors
# ─────────────────────────────────────────────────────────────────────

_RETRY_ATTEMPTS = int(os.environ.get("HELEN_FED_RETRY_ATTEMPTS", "2"))


async def with_retry(coro_factory, *, peer_id: str = "") -> tuple[bool, Optional[Exception]]:
    """Execute `coro_factory()` with one retry on transient failures.

    Returns (ok, last_exception). `peer_id` (optional) drives breaker
    bookkeeping — pass "" to bypass the breaker.
    """
    if peer_id and not await can_attempt(peer_id):
        return False, RuntimeError(f"breaker_open:{peer_id[:12]}")
    last_exc: Optional[Exception] = None
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            result = await coro_factory()
            ok = bool(result)
            if peer_id:
                if ok:
                    await record_success(peer_id)
                else:
                    await record_failure(peer_id)
            if ok:
                return True, None
            # Falsy result = soft failure — don't retry, the peer responded.
            return False, None
        except Exception as exc:
            last_exc = exc
            # Only retry on what looks like a transient error.
            msg = str(exc).lower()
            transient = any(t in msg for t in (
                "timeout", "timed out", "connection", "refused", "reset", "broken pipe",
            ))
            if not transient or attempt == _RETRY_ATTEMPTS - 1:
                break
            # Brief backoff before retry: 100ms, 200ms, ...
            await asyncio.sleep(0.1 * (attempt + 1))
    if peer_id:
        await record_failure(peer_id)
    return False, last_exc


__all__ = [
    "remember_origin",
    "forget_origin",
    "lookup_origin",
    "can_attempt",
    "record_success",
    "record_failure",
    "breaker_snapshot",
    "with_retry",
]
