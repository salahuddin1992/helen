"""Session affinity — sticky routing for stateful clients.

A user's WebSocket session lives on a single Helen-Server. If a
subsequent HTTP request from the same user lands on a *different*
peer, that peer has no in-memory state and has to ask around or
forward — wasted hop.

This module remembers ``user_id → home_node_id`` mappings in the
replicated KV store (``services.replication_manager``) so every
peer agrees on which node owns the session. The mapping has a TTL
that's refreshed by the WebSocket heartbeat; expired entries get
re-bound to the next peer the user lands on.

Usage::

    from app.services.session_affinity import bind, lookup, refresh

    await bind(user_id, my_node_id, ttl_sec=600)
    home = await lookup(user_id)        # → home_node_id or None
    await refresh(user_id, ttl_sec=600) # extend lease
"""

from __future__ import annotations

import os
import threading
import time
from typing import Optional


def _f(env: str, default: float) -> float:
    try:
        return float(os.environ.get(env) or default)
    except (TypeError, ValueError):
        return default


DEFAULT_TTL_SEC = _f("HELEN_AFFINITY_TTL_SEC", 600.0)


class _LocalCache:
    """In-process cache to avoid hitting replication for every event."""
    _lock = threading.RLock()
    _entries: dict[str, tuple[str, float]] = {}  # user_id -> (home_node, expires_at)

    @classmethod
    def get(cls, user_id: str) -> Optional[tuple[str, float]]:
        with cls._lock:
            return cls._entries.get(user_id)

    @classmethod
    def put(cls, user_id: str, home_node: str, expires_at: float) -> None:
        with cls._lock:
            cls._entries[user_id] = (home_node, expires_at)
            if len(cls._entries) > 10_000:
                # Trim oldest 1000.
                sorted_items = sorted(
                    cls._entries.items(), key=lambda kv: kv[1][1],
                )[:1000]
                for k, _ in sorted_items:
                    cls._entries.pop(k, None)

    @classmethod
    def drop(cls, user_id: str) -> None:
        with cls._lock:
            cls._entries.pop(user_id, None)


def _kv_key(user_id: str) -> str:
    return f"affinity::{user_id}"


async def bind(user_id: str, home_node_id: str,
               *, ttl_sec: float = DEFAULT_TTL_SEC) -> bool:
    """Set/refresh the user's home node. Returns True on success."""
    if not user_id or not home_node_id:
        return False
    expires_at = time.time() + ttl_sec
    record = {"home_node": home_node_id, "expires_at": expires_at}
    try:
        from app.services.replication_manager import put as rep_put
        rep_put("affinity", user_id, record)
    except Exception:
        pass  # local-only fallback
    _LocalCache.put(user_id, home_node_id, expires_at)
    return True


async def lookup(user_id: str) -> Optional[str]:
    """Return the user's home_node_id if a non-expired binding exists."""
    if not user_id:
        return None
    cached = _LocalCache.get(user_id)
    if cached:
        node, exp = cached
        if exp > time.time():
            return node
        _LocalCache.drop(user_id)

    # Replication lookup.
    try:
        from app.services.replication_manager import get as rep_get
        rec = rep_get("affinity", user_id)
        if rec and isinstance(rec.get("value"), dict):
            v = rec["value"]
            home_node = v.get("home_node")
            expires_at = float(v.get("expires_at") or 0)
            if home_node and expires_at > time.time():
                _LocalCache.put(user_id, home_node, expires_at)
                return home_node
    except Exception:
        pass
    return None


async def refresh(user_id: str,
                  *, ttl_sec: float = DEFAULT_TTL_SEC) -> bool:
    """Extend a binding without changing the home node."""
    home = await lookup(user_id)
    if not home:
        return False
    return await bind(user_id, home, ttl_sec=ttl_sec)


async def unbind(user_id: str) -> bool:
    """Remove the binding. Subsequent lookups will return None."""
    if not user_id:
        return False
    try:
        from app.services.replication_manager import put as rep_put
        rep_put("affinity", user_id,
                {"home_node": "", "expires_at": 0})
    except Exception:
        pass
    _LocalCache.drop(user_id)
    return True


def is_local(user_id: str) -> bool:
    """Synchronous best-effort check against the local cache only."""
    cached = _LocalCache.get(user_id)
    if cached is None:
        return False
    home_node, expires_at = cached
    if expires_at <= time.time():
        return False
    try:
        from app.services.discovery_service import get_server_id
        return home_node == (get_server_id() or "")
    except Exception:
        return False


def snapshot() -> dict:
    with _LocalCache._lock:
        items = list(_LocalCache._entries.items())
    now = time.time()
    return {
        "default_ttl_sec": DEFAULT_TTL_SEC,
        "cached_count":    len(items),
        "active": [
            {
                "user_id":       uid,
                "home_node":     home_node,
                "expires_in_sec": round(expires_at - now, 1),
            }
            for uid, (home_node, expires_at) in items[:50]
            if expires_at > now
        ],
    }
