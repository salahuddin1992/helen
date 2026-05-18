"""
Router config store — base URL + token resolution with DB override.

Resolution order (highest priority first)
-----------------------------------------
  1. **DB override row** in ``router_control_config`` (one row,
     ``id=1``). Lets an operator change the router URL/token at
     runtime through the admin UI without restarting Helen-Server.
  2. **Environment / Settings**
       * ``HELEN_ROUTER_BASE_URL`` (default
         ``http://router.helen.lan:8080``)
       * ``HELEN_ROUTER_TOKEN``    (required for write ops; an
         empty value means the router will reject the call — we
         still pass it through so the operator sees the real
         error).

The DB row is opportunistic — if the table doesn't exist yet
(fresh install, migrations not run, or running in tests without
the model registered) we silently fall back to env vars. Never
crash the proxy because of a missing config row.

Caching
-------
The resolved (url, token) pair is cached for ``CACHE_TTL_SECONDS``
seconds. After that we re-read the DB row. This lets an admin
rotate the token through ``POST /api/admin/router/security/rotate-token``
without having to bounce the process.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from app.core.logging import get_logger

logger = get_logger(__name__)


# ── Tunables ──────────────────────────────────────────────────────


DEFAULT_BASE_URL = "http://router.helen.lan:8080"
"""The DNS name we publish in the installer's /etc/hosts pinning.

Operators that don't run the installer can override this with the
``HELEN_ROUTER_BASE_URL`` env var or via the DB-backed admin UI."""

CACHE_TTL_SECONDS = 30.0
"""How long a resolved config tuple stays in memory before we
re-check the DB. 30 s is short enough that an admin-initiated
token rotation propagates inside one human-scale interaction, and
long enough that a tight loop of proxied requests doesn't hammer
the DB once per call."""


# ── Cached resolution ────────────────────────────────────────────


@dataclass
class ResolvedConfig:
    """Snapshot of the router connection config at a point in time."""

    base_url: str
    token: str
    source: str  # "db", "env", or "default"
    resolved_at: float = field(default_factory=time.time)

    @property
    def has_token(self) -> bool:
        return bool(self.token)

    @property
    def is_stale(self) -> bool:
        return (time.time() - self.resolved_at) > CACHE_TTL_SECONDS

    def sanitized(self) -> dict[str, Any]:
        """Safe-for-UI projection — never echo back the token."""
        return {
            "base_url": self.base_url,
            "token_set": self.has_token,
            "token_length": len(self.token),
            "source": self.source,
            "resolved_at": self.resolved_at,
            "stale": self.is_stale,
        }


# ── Config store ─────────────────────────────────────────────────


class RouterConfigStore:
    """Async, single-instance config resolver with DB override.

    Use :func:`get_router_config_store` to access the singleton.
    Holds an internal asyncio.Lock so concurrent first-time
    resolution doesn't issue duplicate DB queries.
    """

    def __init__(self) -> None:
        self._cache: Optional[ResolvedConfig] = None
        self._lock = asyncio.Lock()

    # ── Public API ──────────────────────────────────────────────

    async def get(self, *, force_refresh: bool = False) -> ResolvedConfig:
        """Return the current config tuple, refreshing if stale.

        Args:
            force_refresh: skip the cache and re-read the DB.
        """
        if (not force_refresh
                and self._cache is not None
                and not self._cache.is_stale):
            return self._cache

        async with self._lock:
            # Re-check under the lock (the classic double-checked
            # idiom — another coroutine may have just refreshed).
            if (not force_refresh
                    and self._cache is not None
                    and not self._cache.is_stale):
                return self._cache
            cfg = await self._resolve()
            self._cache = cfg
            return cfg

    async def invalidate(self) -> None:
        """Drop the cache. Call after a write to the override row."""
        async with self._lock:
            self._cache = None

    async def set_override(
        self, base_url: str, token: str | None,
    ) -> ResolvedConfig:
        """Persist a runtime override and return the new config.

        ``token=None`` clears just the URL override and falls back
        to the env token. ``token=""`` clears both.
        """
        await self._write_override(base_url, token)
        await self.invalidate()
        return await self.get(force_refresh=True)

    async def get_url(self) -> str:
        return (await self.get()).base_url

    async def get_token(self) -> str:
        return (await self.get()).token

    # ── Resolution internals ────────────────────────────────────

    async def _resolve(self) -> ResolvedConfig:
        # 1. Try DB override
        try:
            db_cfg = await self._read_override()
            if db_cfg is not None:
                url, token = db_cfg
                if url:
                    return ResolvedConfig(
                        base_url=url,
                        token=token or self._env_token(),
                        source="db",
                    )
        except Exception as exc:
            # Never let a DB hiccup break the proxy
            logger.debug(
                "router_config_db_read_failed",
                error=str(exc),
            )

        # 2. Env vars
        env_url = (os.environ.get("HELEN_ROUTER_BASE_URL") or "").strip()
        env_token = self._env_token()
        if env_url:
            return ResolvedConfig(
                base_url=env_url.rstrip("/"),
                token=env_token,
                source="env",
            )

        # 3. Built-in default
        return ResolvedConfig(
            base_url=DEFAULT_BASE_URL,
            token=env_token,
            source="default",
        )

    @staticmethod
    def _env_token() -> str:
        return (os.environ.get("HELEN_ROUTER_TOKEN") or "").strip()

    async def _read_override(self) -> Optional[tuple[str, str]]:
        """Read the single override row from the DB.

        Returns ``(base_url, token)`` or ``None`` if no row exists
        or the table is unavailable.
        """
        try:
            from sqlalchemy import select  # local import — keep
            from app.db.session import async_session_factory
        except Exception:
            return None

        # Lazy model import — fall back gracefully if it doesn't exist.
        try:
            from app.models.router_control_config import (
                RouterControlConfig,
            )
        except Exception:
            return None

        try:
            async with async_session_factory() as db:
                row = (await db.execute(
                    select(RouterControlConfig)
                    .where(RouterControlConfig.id == 1)
                )).scalar_one_or_none()
                if row is None:
                    return None
                url = (row.base_url or "").strip().rstrip("/")
                token = (row.token or "").strip()
                if not url:
                    return None
                return url, token
        except Exception as exc:
            logger.debug(
                "router_config_override_query_failed",
                error=str(exc),
            )
            return None

    async def _write_override(
        self, base_url: str, token: str | None,
    ) -> None:
        """Upsert the override row."""
        base_url = (base_url or "").strip().rstrip("/")
        if not base_url:
            raise ValueError("base_url cannot be empty")

        try:
            from sqlalchemy import select
            from app.db.session import async_session_factory
            from app.models.router_control_config import (
                RouterControlConfig,
            )
        except Exception as exc:
            raise RuntimeError(
                "router_control_config DB model unavailable — "
                "run migrations or set HELEN_ROUTER_BASE_URL via env",
            ) from exc

        async with async_session_factory() as db:
            row = (await db.execute(
                select(RouterControlConfig)
                .where(RouterControlConfig.id == 1)
            )).scalar_one_or_none()
            if row is None:
                row = RouterControlConfig(
                    id=1,
                    base_url=base_url,
                    token=token or "",
                )
                db.add(row)
            else:
                row.base_url = base_url
                if token is not None:
                    row.token = token or ""
            await db.commit()


# ── Singleton accessor ───────────────────────────────────────────


_store: Optional[RouterConfigStore] = None


def get_router_config_store() -> RouterConfigStore:
    """Return the process-wide :class:`RouterConfigStore`."""
    global _store
    if _store is None:
        _store = RouterConfigStore()
    return _store
