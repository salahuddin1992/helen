"""
Phase 6 / Module AC — pluggable distributed session store.

A unified async API used by every component that needs to share volatile
state across cluster nodes:

* Refresh-token / device-session bookkeeping
* Socket.io sticky-routing hints
* Short-lived auth challenges (TOTP setup, OAuth state, pairing tickets)
* Rate-limiter and IDS counters when a Redis backend is available

Three concrete implementations are shipped:

* ``RedisSessionStore``     — preferred for multi-node deployments
* ``SQLSessionStore``       — uses the existing app DB; works without Redis
* ``InMemorySessionStore``  — single-node fallback; zero dependencies

The selection is driven by ``settings.REDIS_URL`` and an explicit
``SESSION_STORE_BACKEND`` override (set via env). All optional imports
are wrapped so absence of ``redis.asyncio`` never crashes a deployment.
"""
from __future__ import annotations

import abc
import asyncio
import json
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.session import async_session_factory

logger = get_logger(__name__)


# ── interface ───────────────────────────────────────────────


class SessionStore(abc.ABC):
    """Abstract async key/value store with TTLs."""

    @abc.abstractmethod
    async def get(self, key: str) -> Optional[Any]:
        ...

    @abc.abstractmethod
    async def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        ...

    @abc.abstractmethod
    async def delete(self, key: str) -> None:
        ...

    @abc.abstractmethod
    async def expire(self, key: str, ttl: int) -> bool:
        ...

    async def health(self) -> dict[str, Any]:
        """Optional override — reported by /api/admin/cluster/health."""
        return {"backend": self.__class__.__name__, "ok": True}


# ── in-memory fallback ──────────────────────────────────────


class InMemorySessionStore(SessionStore):
    """Single-process, single-node fallback. Suitable for dev/small ops."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._d: dict[str, tuple[Any, Optional[float]]] = {}

    def _alive(self, exp: Optional[float]) -> bool:
        return exp is None or exp > time.time()

    async def get(self, key: str) -> Optional[Any]:
        async with self._lock:
            entry = self._d.get(key)
            if not entry:
                return None
            val, exp = entry
            if not self._alive(exp):
                self._d.pop(key, None)
                return None
            return val

    async def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        exp = time.time() + ttl if ttl else None
        async with self._lock:
            self._d[key] = (value, exp)

    async def delete(self, key: str) -> None:
        async with self._lock:
            self._d.pop(key, None)

    async def expire(self, key: str, ttl: int) -> bool:
        async with self._lock:
            entry = self._d.get(key)
            if not entry:
                return False
            self._d[key] = (entry[0], time.time() + ttl)
            return True

    async def health(self) -> dict[str, Any]:
        return {"backend": "memory", "ok": True, "size": len(self._d)}


# ── redis backend (optional) ────────────────────────────────


class RedisSessionStore(SessionStore):
    """Redis-backed store. Values are JSON-encoded for portability."""

    def __init__(self, url: str, namespace: str = "helen:sess") -> None:
        try:
            import redis.asyncio as redis  # type: ignore
        except Exception as exc:                                    # pragma: no cover
            raise RuntimeError(
                "RedisSessionStore selected but redis.asyncio is not installed"
            ) from exc
        self._redis = redis.from_url(url, decode_responses=True)
        self._ns = namespace.rstrip(":")

    def _k(self, key: str) -> str:
        return f"{self._ns}:{key}"

    async def get(self, key: str) -> Optional[Any]:
        raw = await self._redis.get(self._k(key))
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return raw

    async def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        payload = json.dumps(value, default=str) if not isinstance(value, str) else value
        if ttl:
            await self._redis.set(self._k(key), payload, ex=ttl)
        else:
            await self._redis.set(self._k(key), payload)

    async def delete(self, key: str) -> None:
        await self._redis.delete(self._k(key))

    async def expire(self, key: str, ttl: int) -> bool:
        return bool(await self._redis.expire(self._k(key), ttl))

    async def health(self) -> dict[str, Any]:
        try:
            pong = await self._redis.ping()
            return {"backend": "redis", "ok": bool(pong)}
        except Exception as exc:                                    # pragma: no cover
            return {"backend": "redis", "ok": False, "error": str(exc)}


# ── SQL backend ─────────────────────────────────────────────

_SQL_DDL = """
CREATE TABLE IF NOT EXISTS cluster_session_kv (
    k VARCHAR(255) PRIMARY KEY,
    v TEXT NOT NULL,
    expires_at TIMESTAMP NULL
);
"""

_SQL_IDX = """
CREATE INDEX IF NOT EXISTS ix_cluster_session_kv_exp
ON cluster_session_kv (expires_at);
"""


class SQLSessionStore(SessionStore):
    """Uses the existing database. Cheap for low-write workloads, and
    avoids requiring Redis when running a 2–3 node deployment."""

    def __init__(self) -> None:
        self._bootstrapped = False
        self._bs_lock = asyncio.Lock()

    async def _ensure_table(self, db: AsyncSession) -> None:
        if self._bootstrapped:
            return
        async with self._bs_lock:
            if self._bootstrapped:
                return
            try:
                await db.execute(text(_SQL_DDL))
                await db.execute(text(_SQL_IDX))
                await db.commit()
            except Exception:                                       # pragma: no cover
                await db.rollback()
            self._bootstrapped = True

    async def _session(self) -> AsyncSession:
        return async_session_factory()

    async def get(self, key: str) -> Optional[Any]:
        async with async_session_factory() as db:
            await self._ensure_table(db)
            row = (await db.execute(
                text("SELECT v, expires_at FROM cluster_session_kv WHERE k=:k"),
                {"k": key},
            )).first()
            if not row:
                return None
            v, exp = row
            if exp is not None:
                if isinstance(exp, str):
                    try:
                        exp = datetime.fromisoformat(exp)
                    except Exception:
                        exp = None
                if exp is not None and exp <= datetime.now(timezone.utc):
                    await db.execute(
                        text("DELETE FROM cluster_session_kv WHERE k=:k"),
                        {"k": key},
                    )
                    await db.commit()
                    return None
            try:
                return json.loads(v)
            except Exception:
                return v

    async def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        payload = json.dumps(value, default=str) if not isinstance(value, str) else value
        exp = (datetime.now(timezone.utc) + timedelta(seconds=ttl)) if ttl else None
        async with async_session_factory() as db:
            await self._ensure_table(db)
            await db.execute(
                text("DELETE FROM cluster_session_kv WHERE k=:k"),
                {"k": key},
            )
            await db.execute(
                text(
                    "INSERT INTO cluster_session_kv (k, v, expires_at) "
                    "VALUES (:k, :v, :exp)"
                ),
                {"k": key, "v": payload, "exp": exp},
            )
            await db.commit()

    async def delete(self, key: str) -> None:
        async with async_session_factory() as db:
            await self._ensure_table(db)
            await db.execute(
                text("DELETE FROM cluster_session_kv WHERE k=:k"),
                {"k": key},
            )
            await db.commit()

    async def expire(self, key: str, ttl: int) -> bool:
        new_exp = datetime.now(timezone.utc) + timedelta(seconds=ttl)
        async with async_session_factory() as db:
            await self._ensure_table(db)
            res = await db.execute(
                text(
                    "UPDATE cluster_session_kv SET expires_at=:exp WHERE k=:k"
                ),
                {"k": key, "exp": new_exp},
            )
            await db.commit()
            return (res.rowcount or 0) > 0

    async def health(self) -> dict[str, Any]:
        try:
            async with async_session_factory() as db:
                await self._ensure_table(db)
                row = (await db.execute(
                    text("SELECT COUNT(*) FROM cluster_session_kv")
                )).scalar_one()
                return {"backend": "sql", "ok": True, "size": int(row or 0)}
        except Exception as exc:                                    # pragma: no cover
            return {"backend": "sql", "ok": False, "error": str(exc)}


# ── module-level resolver ───────────────────────────────────


_singleton: Optional[SessionStore] = None
_singleton_lock = asyncio.Lock()


async def get_session_store() -> SessionStore:
    """Return the configured store. Idempotent."""
    global _singleton
    if _singleton is not None:
        return _singleton
    async with _singleton_lock:
        if _singleton is not None:
            return _singleton
        settings = get_settings()
        backend = (os.environ.get("SESSION_STORE_BACKEND")
                   or ("redis" if settings.REDIS_URL else "sql")).lower()
        if backend == "redis" and settings.REDIS_URL:
            try:
                _singleton = RedisSessionStore(settings.REDIS_URL)
                logger.info("session_store: using Redis")
            except Exception as exc:
                logger.warning("session_store: redis init failed (%s) → falling back to SQL", exc)
                _singleton = SQLSessionStore()
        elif backend == "memory":
            _singleton = InMemorySessionStore()
            logger.info("session_store: using in-memory backend")
        else:
            _singleton = SQLSessionStore()
            logger.info("session_store: using SQL backend")
        return _singleton
