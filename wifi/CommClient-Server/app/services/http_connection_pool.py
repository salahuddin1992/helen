"""HTTP connection pool — keep-alive client per peer.

Every relay/proxy/federation call currently opens a fresh
``httpx.AsyncClient``. Each client opens a new TCP connection
(plus TLS handshake when applicable). On a busy mesh we end up
paying the TCP/TLS setup cost over and over for the same peer.

This pool keeps one ``httpx.AsyncClient`` per peer base URL with
sensible defaults (HTTP/1.1 keep-alive + connection pooling). Idle
clients are reaped after ``IDLE_TIMEOUT_SEC`` seconds so we don't
hold connections to peers we no longer talk to.

Use::

    from app.services.http_connection_pool import get_pool
    client = await get_pool().client_for("http://1.2.3.4:3000")
    r = await client.get("/api/cluster/info")
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from dataclasses import dataclass
from typing import Optional


def _f(env: str, default: float) -> float:
    try:
        return float(os.environ.get(env) or default)
    except (TypeError, ValueError):
        return default


def _i(env: str, default: int) -> int:
    try:
        return int(os.environ.get(env) or default)
    except (TypeError, ValueError):
        return default


IDLE_TIMEOUT_SEC = _f("HELEN_HTTP_POOL_IDLE_SEC", 60.0)
MAX_CONNECTIONS  = _i("HELEN_HTTP_POOL_MAX_CONNS", 50)
MAX_KEEPALIVE    = _i("HELEN_HTTP_POOL_MAX_KEEPALIVE", 25)
DEFAULT_TIMEOUT  = _f("HELEN_HTTP_POOL_TIMEOUT_SEC", 5.0)
ENABLE_HTTP2     = (os.environ.get("HELEN_HTTP_POOL_HTTP2", "0").lower()
                    in ("1", "true", "yes"))
USER_AGENT       = os.environ.get(
    "HELEN_HTTP_POOL_UA", "CommClient-Server/1.0 (federation pool)",
)


@dataclass
class _PooledClient:
    base_url:    str
    client:      "object"   # httpx.AsyncClient
    created_at:  float
    last_used_at: float
    requests:    int = 0


class HTTPConnectionPool:
    _singleton: "HTTPConnectionPool | None" = None

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._clients: dict[str, _PooledClient] = {}
        self._loop_task: Optional[asyncio.Task] = None
        self._running = False

    @classmethod
    def instance(cls) -> "HTTPConnectionPool":
        if cls._singleton is None:
            cls._singleton = HTTPConnectionPool()
        return cls._singleton

    # ── Public API ─────────────────────────────────────────

    async def client_for(self, base_url: str,
                          *, timeout: Optional[float] = None) -> object:
        """Return an httpx.AsyncClient bound to base_url. Creates +
        caches one if missing."""
        try:
            import httpx
        except ImportError:
            raise RuntimeError("httpx not installed")

        with self._lock:
            entry = self._clients.get(base_url)
            if entry is not None:
                entry.last_used_at = time.time()
                entry.requests += 1
                return entry.client

        # Build a new client outside the lock.
        limits = httpx.Limits(
            max_connections=MAX_CONNECTIONS,
            max_keepalive_connections=MAX_KEEPALIVE,
        )
        client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout or DEFAULT_TIMEOUT,
            limits=limits,
            http2=ENABLE_HTTP2,
            headers={"User-Agent": USER_AGENT},
        )

        with self._lock:
            existing = self._clients.get(base_url)
            if existing is not None:
                # Race: another caller built one first; close ours.
                try:
                    await client.aclose()
                except Exception:
                    pass
                existing.last_used_at = time.time()
                existing.requests += 1
                return existing.client
            now = time.time()
            self._clients[base_url] = _PooledClient(
                base_url=base_url, client=client,
                created_at=now, last_used_at=now,
                requests=1,
            )
        return client

    async def close_idle(self) -> int:
        """Reap clients idle > IDLE_TIMEOUT_SEC. Returns the count
        actually closed."""
        cutoff = time.time() - IDLE_TIMEOUT_SEC
        to_close: list[_PooledClient] = []
        with self._lock:
            keep: dict[str, _PooledClient] = {}
            for k, e in self._clients.items():
                if e.last_used_at < cutoff:
                    to_close.append(e)
                else:
                    keep[k] = e
            self._clients = keep
        for e in to_close:
            try:
                await e.client.aclose()  # type: ignore[attr-defined]
            except Exception:
                pass
        return len(to_close)

    async def close_all(self) -> None:
        with self._lock:
            entries = list(self._clients.values())
            self._clients.clear()
        for e in entries:
            try:
                await e.client.aclose()  # type: ignore[attr-defined]
            except Exception:
                pass

    # ── Reaper loop ────────────────────────────────────────

    async def _run_loop(self) -> None:
        self._running = True
        try:
            while self._running:
                try:
                    await self.close_idle()
                except Exception:
                    pass
                await asyncio.sleep(IDLE_TIMEOUT_SEC / 2)
        finally:
            pass

    def start(self) -> None:
        if self._loop_task is not None and not self._loop_task.done():
            return
        try:
            loop = asyncio.get_event_loop()
            self._loop_task = loop.create_task(
                self._run_loop(), name="http-conn-pool-reaper",
            )
        except RuntimeError:
            pass

    def stop(self) -> None:
        self._running = False
        if self._loop_task is not None:
            self._loop_task.cancel()
            self._loop_task = None

    # ── Diagnostics ────────────────────────────────────────

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "pool_size":     len(self._clients),
                "max_connections": MAX_CONNECTIONS,
                "max_keepalive": MAX_KEEPALIVE,
                "idle_timeout":  IDLE_TIMEOUT_SEC,
                "clients": [
                    {
                        "base_url":     c.base_url,
                        "requests":     c.requests,
                        "age_sec":      round(time.time() - c.created_at, 1),
                        "idle_sec":     round(time.time() - c.last_used_at, 1),
                    }
                    for c in self._clients.values()
                ],
            }


def get_pool() -> HTTPConnectionPool:
    return HTTPConnectionPool.instance()
