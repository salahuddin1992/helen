"""Distributed rate limiter — cluster-wide token bucket per identity.

Per-node rate limits leak: a malicious user just spreads requests
across the cluster to bypass them. This module keeps the bucket
state in the existing replicated KV store
(``services.replication_manager``) so every peer sees the same
counter regardless of which node the request hit.

Usage::

    allowed = await rate_limit("user:42", limit_per_minute=60)
    if not allowed:
        raise HTTPException(429)

The replicated state is *eventually* consistent — under partition
two halves can briefly admit double the budget, then re-merge via
LWW + max(). For local-only stricter limits, layer a per-node
bucket on top.
"""

from __future__ import annotations

import math
import os
import threading
import time
from dataclasses import dataclass


def _i(env: str, default: int) -> int:
    try:
        return int(os.environ.get(env) or default)
    except (TypeError, ValueError):
        return default


WINDOW_SEC = _i("HELEN_DRL_WINDOW_SEC", 60)


@dataclass
class RateBucket:
    identity:  str
    count:     int
    window_at: float


class _LocalCache:
    """Tiny in-process cache to avoid hitting replication on every
    request. Not authoritative — only used for fast deny path."""
    _lock = threading.RLock()
    _cache: dict[str, RateBucket] = {}

    @classmethod
    def get(cls, key: str) -> RateBucket | None:
        with cls._lock:
            return cls._cache.get(key)

    @classmethod
    def put(cls, b: RateBucket) -> None:
        with cls._lock:
            cls._cache[b.identity] = b
            # Trim to 5000 entries to bound memory.
            if len(cls._cache) > 5000:
                # Evict the oldest 500.
                victims = sorted(
                    cls._cache.items(),
                    key=lambda kv: kv[1].window_at,
                )[:500]
                for k, _ in victims:
                    cls._cache.pop(k, None)


def _kv_key(identity: str) -> str:
    return f"ratelimit::{identity}"


async def rate_limit(identity: str, *, limit_per_minute: int,
                     cost: int = 1) -> bool:
    """Returns True if the request is allowed, False if exhausted.

    Identity is opaque — caller decides ``user:42`` or ``ip:1.2.3.4``
    or ``api_key:xyz``.
    """
    if not identity or limit_per_minute <= 0 or cost <= 0:
        return True

    now = time.time()
    cache_hit = _LocalCache.get(identity)
    # Fast deny: if local cache says we're over and the window hasn't
    # rolled, no need to round-trip to replication.
    if cache_hit and now - cache_hit.window_at < WINDOW_SEC:
        if cache_hit.count + cost > limit_per_minute:
            return False

    try:
        from app.services.replication_manager import get as rep_get, put as rep_put
    except ImportError:
        # Replication unavailable — fall back to local-only.
        if cache_hit and now - cache_hit.window_at < WINDOW_SEC:
            if cache_hit.count + cost > limit_per_minute:
                return False
            cache_hit.count += cost
            _LocalCache.put(cache_hit)
            return True
        _LocalCache.put(RateBucket(identity, cost, now))
        return True

    # Read latest replicated bucket.
    rec = rep_get("ratelimit", identity)
    if rec and isinstance(rec.get("value"), dict):
        v = rec["value"]
        cur_count = int(v.get("count") or 0)
        cur_window = float(v.get("window_at") or now)
        if now - cur_window >= WINDOW_SEC:
            cur_count = 0
            cur_window = now
    else:
        cur_count = 0
        cur_window = now

    if cur_count + cost > limit_per_minute:
        # Block, but still update local cache so subsequent calls
        # short-circuit without hitting replication.
        _LocalCache.put(RateBucket(identity, cur_count, cur_window))
        return False

    new_count = cur_count + cost
    rep_put("ratelimit", identity,
            {"count": new_count, "window_at": cur_window})
    _LocalCache.put(RateBucket(identity, new_count, cur_window))
    return True


def remaining(identity: str, *, limit_per_minute: int) -> int:
    cache = _LocalCache.get(identity)
    if cache is None:
        return limit_per_minute
    if time.time() - cache.window_at >= WINDOW_SEC:
        return limit_per_minute
    return max(0, limit_per_minute - cache.count)


def peek(identity: str, *, limit_per_minute: int) -> dict:
    """Read current bucket state without consuming budget. Useful for
    middleware that emits ``X-RateLimit-Remaining`` headers and for
    admin diagnostics. Falls back to local cache if replication is
    unavailable."""
    if not identity or limit_per_minute <= 0:
        return {
            "identity": identity, "limit": limit_per_minute,
            "used": 0, "remaining": limit_per_minute,
            "reset_in_sec": 0.0, "source": "noop",
        }
    now = time.time()
    cur_count = 0
    cur_window = now
    source = "cache"
    try:
        from app.services.replication_manager import get as rep_get
        rec = rep_get("ratelimit", identity)
        if rec and isinstance(rec.get("value"), dict):
            v = rec["value"]
            cur_count = int(v.get("count") or 0)
            cur_window = float(v.get("window_at") or now)
            source = "replicated"
    except Exception:
        cache = _LocalCache.get(identity)
        if cache is not None:
            cur_count = cache.count
            cur_window = cache.window_at
    if now - cur_window >= WINDOW_SEC:
        cur_count = 0
        cur_window = now
    return {
        "identity":    identity,
        "limit":       limit_per_minute,
        "used":        cur_count,
        "remaining":   max(0, limit_per_minute - cur_count),
        "reset_in_sec": max(0.0, round(WINDOW_SEC - (now - cur_window), 2)),
        "source":      source,
    }


def reset(identity: str) -> bool:
    """Admin-only: forcibly clear the bucket for ``identity`` (e.g.
    operator marks an over-eager service account as recovered).
    Returns True if any state was cleared."""
    if not identity:
        return False
    cleared = False
    with _LocalCache._lock:
        if _LocalCache._cache.pop(identity, None) is not None:
            cleared = True
    try:
        from app.services.replication_manager import put as rep_put
        rep_put("ratelimit", identity, {"count": 0, "window_at": time.time()})
        cleared = True
    except Exception:
        pass
    return cleared


def snapshot() -> dict:
    with _LocalCache._lock:
        items = list(_LocalCache._cache.values())
    now = time.time()
    return {
        "window_sec":   WINDOW_SEC,
        "cached_items": len(items),
        "active": [
            {
                "identity": b.identity,
                "count":    b.count,
                "age_sec":  round(now - b.window_at, 1),
            }
            for b in items
            if now - b.window_at < WINDOW_SEC
        ][:50],
    }
