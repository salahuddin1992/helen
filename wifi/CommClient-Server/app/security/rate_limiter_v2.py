"""
Phase 6 / Module AE — Rate limiter v2 (hybrid sliding-window + token-bucket).

Three independent limits:

* Per-IP   — defends against scrapers and brute-forcers
* Per-user — fair-share among authenticated users
* Per-route — additional cap on expensive endpoints

Sliding-window-log governs steady-state behaviour; a token-bucket
allows short bursts above the steady rate. Whitelist via CIDR.

Distributed mode uses Redis (INCR + EXPIRE) when ``SessionStore`` is
backed by Redis; otherwise local async memory.
"""
from __future__ import annotations

import asyncio
import ipaddress
import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.logging import get_logger
from app.observability.metrics_exporter import counter_inc

logger = get_logger(__name__)


@dataclass
class Limit:
    capacity: int           # tokens (= max sustained burst)
    refill_per_sec: float   # steady-state rate
    window_seconds: int = 60


@dataclass
class RateLimitConfig:
    per_ip: Limit = field(default_factory=lambda: Limit(120, 2.0, 60))
    per_user: Limit = field(default_factory=lambda: Limit(300, 5.0, 60))
    per_route: dict[str, Limit] = field(default_factory=lambda: {
        "/api/auth/login": Limit(10, 0.2, 60),
        "/api/auth/register": Limit(5, 0.1, 300),
        "/api/auth/refresh": Limit(30, 1.0, 60),
        "/api/files/upload": Limit(60, 2.0, 60),
    })
    cidr_whitelist: list[str] = field(default_factory=lambda: [
        "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8",
    ])


# ── local async memory state ────────────────────────────────


class _LocalBucket:
    __slots__ = ("tokens", "last", "window")

    def __init__(self, capacity: int):
        self.tokens = float(capacity)
        self.last = time.monotonic()
        self.window: deque[float] = deque()

    def consume(self, cap: int, refill: float, window: int) -> tuple[bool, float]:
        now = time.monotonic()
        # refill
        elapsed = now - self.last
        self.tokens = min(cap, self.tokens + elapsed * refill)
        self.last = now
        # window log: drop old
        while self.window and now - self.window[0] > window:
            self.window.popleft()
        # require both tokens AND under window count
        max_count_in_window = int(cap + refill * window)
        if self.tokens < 1.0 or len(self.window) >= max_count_in_window:
            wait = max(0.0, (1.0 - self.tokens) / refill)
            return False, wait
        self.tokens -= 1.0
        self.window.append(now)
        return True, 0.0


class _LocalState:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.buckets: dict[str, _LocalBucket] = {}

    async def check(self, key: str, limit: Limit) -> tuple[bool, float, int]:
        async with self.lock:
            b = self.buckets.get(key)
            if b is None:
                b = _LocalBucket(limit.capacity)
                self.buckets[key] = b
            ok, wait = b.consume(limit.capacity, limit.refill_per_sec,
                                 limit.window_seconds)
            remaining = int(b.tokens)
            return ok, wait, remaining


# ── redis state ─────────────────────────────────────────────


class _RedisState:
    def __init__(self, redis_client) -> None:
        self._r = redis_client

    async def check(self, key: str, limit: Limit) -> tuple[bool, float, int]:
        # Sliding-window log using ZSET + ZCARD + ZADD + EXPIRE.
        now = time.time()
        cutoff = now - limit.window_seconds
        zkey = f"helen:rl:{key}"
        try:
            pipe = self._r.pipeline()
            pipe.zremrangebyscore(zkey, 0, cutoff)
            pipe.zadd(zkey, {f"{now}-{os.urandom(4).hex()}": now})
            pipe.zcard(zkey)
            pipe.expire(zkey, limit.window_seconds + 1)
            _, _, count, _ = await pipe.execute()
            cap = int(limit.capacity + limit.refill_per_sec * limit.window_seconds)
            if int(count) > cap:
                wait = max(0.0, 1.0 / max(limit.refill_per_sec, 0.01))
                return False, wait, max(0, cap - int(count))
            remaining = max(0, cap - int(count))
            return True, 0.0, remaining
        except Exception as exc:                                    # pragma: no cover
            logger.warning("ratelimit: redis err (%s); allowing", exc)
            return True, 0.0, 0


# ── ASGI middleware ─────────────────────────────────────────


class RateLimiterV2:
    def __init__(
        self,
        app: ASGIApp,
        config: Optional[RateLimitConfig] = None,
    ) -> None:
        self.app = app
        self.cfg = config or RateLimitConfig()
        self._whitelist_nets = [ipaddress.ip_network(c, strict=False)
                                for c in self.cfg.cidr_whitelist]
        self._local = _LocalState()
        self._redis_state: Optional[_RedisState] = None
        self._init_task: Optional[asyncio.Task[None]] = None

    async def _ensure_redis(self) -> None:
        if self._redis_state is not None:
            return
        if self._init_task is None:
            self._init_task = asyncio.create_task(self._init_redis())
        try:
            await asyncio.wait_for(asyncio.shield(self._init_task), timeout=0.05)
        except asyncio.TimeoutError:
            pass

    async def _init_redis(self) -> None:
        try:
            from app.services.cluster.session_store import (
                RedisSessionStore, get_session_store,
            )
            store = await get_session_store()
            if isinstance(store, RedisSessionStore):
                self._redis_state = _RedisState(store._redis)  # type: ignore[attr-defined]
        except Exception:                                           # pragma: no cover
            self._redis_state = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        await self._ensure_redis()

        ip = self._extract_ip(scope)
        if self._is_whitelisted(ip):
            await self.app(scope, receive, send)
            return

        path = scope.get("path") or ""
        user_id = self._extract_user_id(scope)

        results: list[tuple[bool, float, int, str]] = []
        results.append((*(await self._check(f"ip:{ip}", self.cfg.per_ip)), "ip"))
        if user_id:
            results.append((*(await self._check(f"user:{user_id}", self.cfg.per_user)), "user"))
        route_limit = self._match_route(path)
        if route_limit is not None:
            results.append((*(await self._check(f"route:{path}:{ip}", route_limit)), "route"))

        denied = next(((ok, wait, rem, scope_lbl)
                       for (ok, wait, rem, scope_lbl) in results if not ok), None)
        if denied is not None:
            _, wait, rem, scope_lbl = denied
            counter_inc("ratelimit_rejected_total", scope=scope_lbl)
            await self._respond_429(send, retry_after=wait, remaining=rem)
            return

        await self.app(scope, receive, send)

    # ── helpers ─────────────────────────────────────────────

    def _extract_ip(self, scope: Scope) -> str:
        # honour X-Forwarded-For if present
        for k, v in (scope.get("headers") or []):
            if k.lower() == b"x-forwarded-for":
                try:
                    return v.decode("latin-1", "ignore").split(",")[0].strip()
                except Exception:                                   # pragma: no cover
                    pass
        client = scope.get("client")
        if client and len(client) >= 1:
            return client[0]
        return "0.0.0.0"

    def _extract_user_id(self, scope: Scope) -> Optional[str]:
        # Look for a header injected upstream by the auth middleware
        for k, v in (scope.get("headers") or []):
            if k.lower() == b"x-user-id":
                try:
                    return v.decode("ascii", "ignore")
                except Exception:                                   # pragma: no cover
                    return None
        return None

    def _is_whitelisted(self, ip: str) -> bool:
        try:
            addr = ipaddress.ip_address(ip)
            return any(addr in n for n in self._whitelist_nets)
        except Exception:
            return False

    def _match_route(self, path: str) -> Optional[Limit]:
        # exact then prefix
        if path in self.cfg.per_route:
            return self.cfg.per_route[path]
        best: Optional[Limit] = None
        for k, lim in self.cfg.per_route.items():
            if path.startswith(k.rstrip("/")):
                best = lim
        return best

    async def _check(self, key: str, lim: Limit) -> tuple[bool, float, int]:
        if self._redis_state is not None:
            return await self._redis_state.check(key, lim)
        return await self._local.check(key, lim)

    async def _respond_429(self, send: Send, retry_after: float, remaining: int) -> None:
        import json
        body = json.dumps({
            "detail": "rate limit exceeded",
            "retry_after": round(retry_after, 3),
        }).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": 429,
            "headers": [
                (b"content-type", b"application/json"),
                (b"retry-after", str(int(max(1, retry_after))).encode("ascii")),
                (b"x-ratelimit-remaining", str(max(0, remaining)).encode("ascii")),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        })
        await send({"type": "http.response.body", "body": body})


_singleton: Optional[RateLimiterV2] = None


def get_rate_limiter() -> Optional[RateLimiterV2]:
    return _singleton


def attach_rate_limiter_v2(app, config: Optional[RateLimitConfig] = None) -> RateLimiterV2:
    global _singleton
    rl = RateLimiterV2(app, config)
    _singleton = rl
    return rl
