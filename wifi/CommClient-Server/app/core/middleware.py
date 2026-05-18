"""
Security middleware for the FastAPI application.

Provides:
  - Security headers (X-Content-Type-Options, X-Frame-Options, etc.)
  - Request size limiting
  - Login brute-force protection (IP-based rate limiting + account lockout)
  - Request ID injection for audit trail
  - Timing-safe error responses
  - Mandatory-router enforcement (HELEN_REQUIRE_ROUTER=1)
"""

from __future__ import annotations

import os
import secrets
import time
from collections import defaultdict
from typing import Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from app.core.logging import get_logger

logger = get_logger(__name__)


# ── Mandatory-router middleware ─────────────────────────────────────

class RouterRequiredMiddleware(BaseHTTPMiddleware):
    """Reject any request that did not transit Helen-Router.

    Activated by ``HELEN_REQUIRE_ROUTER=1`` on the server. The router
    stamps every forwarded request with::

        X-Forwarded-By: helen-router/<HELEN_ROUTER_TOKEN>

    The token is shared between the router and every server. If the
    header is missing or the token doesn't match, the request is
    rejected with HTTP 403.

    Bypassed paths
    --------------
    A handful of routes must remain reachable without the router so
    operators can recover from misconfigurations:

      * ``/api/health`` — load balancers / health monitors
      * ``/router/*``   — router management API (the router itself
                          calls these on a sibling server)
    """

    BYPASS_PREFIXES = (
        "/api/health",
        "/router/",
    )

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)
        self._enabled = (
            os.environ.get("HELEN_REQUIRE_ROUTER", "0").lower()
            in ("1", "true", "yes")
        )

        # Multi-token: any router that knows ANY of these tokens is
        # accepted. Sources merged:
        #   - HELEN_ROUTER_TOKEN  (single)
        #   - HELEN_ROUTER_TOKENS (CSV — values only, OR url=tok pairs)
        tokens: set[str] = set()
        single = (os.environ.get("HELEN_ROUTER_TOKEN") or "").strip()
        if single:
            tokens.add(single)
        many = (os.environ.get("HELEN_ROUTER_TOKENS") or "").strip()
        if many:
            for raw in many.split(","):
                raw = raw.strip()
                if not raw:
                    continue
                # url=tok pair → take the tok side; bare token → use as-is
                tok = raw.split("=", 1)[1].strip() if "=" in raw else raw
                if tok:
                    tokens.add(tok)
        self._tokens = tokens
        self._expected_set = {f"helen-router/{t}" for t in tokens}

        if self._enabled and not tokens:
            logger.error(
                "router_required_but_no_token",
                detail="HELEN_REQUIRE_ROUTER=1 but neither "
                       "HELEN_ROUTER_TOKEN nor HELEN_ROUTER_TOKENS is "
                       "set — every request will be rejected",
            )

    async def dispatch(self, request: Request, call_next: Callable):
        if not self._enabled:
            return await call_next(request)

        path = request.url.path
        if any(path.startswith(p) for p in self.BYPASS_PREFIXES):
            return await call_next(request)

        forwarded = request.headers.get("X-Forwarded-By", "")
        # Constant-time comparison against EVERY accepted token so the
        # number of trusted routers doesn't leak via timing.
        accepted = False
        for expected in self._expected_set:
            if secrets.compare_digest(forwarded, expected):
                accepted = True
                # don't break — keep the loop balanced for timing-safety
        if not self._tokens or not accepted:
            client = request.client.host if request.client else "?"
            logger.warning(
                "router_required_rejected",
                remote=client, path=path,
                forwarded_present=bool(forwarded),
            )
            return JSONResponse(
                {
                    "error": "router_required",
                    "reason": "this Helen-Server only accepts traffic "
                              "via Helen-Router. Direct LAN connections "
                              "are blocked.",
                },
                status_code=403,
            )

        return await call_next(request)


# ── Security Headers Middleware ──────────────────────────

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Inject security headers into every response."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        response = await call_next(request)

        # Prevent MIME-type sniffing
        response.headers["X-Content-Type-Options"] = "nosniff"
        # Prevent clickjacking
        response.headers["X-Frame-Options"] = "DENY"
        # XSS protection (legacy browsers)
        response.headers["X-XSS-Protection"] = "1; mode=block"
        # No referrer leakage
        response.headers["Referrer-Policy"] = "no-referrer"
        # Prevent caching of sensitive responses
        if request.url.path.startswith("/api/auth"):
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
            response.headers["Pragma"] = "no-cache"
        # Permissions policy — disable unnecessary browser APIs
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), payment=()"
        )

        return response


# ── Request Size Limiter ─────────────────────────────────

class RequestSizeLimitMiddleware(BaseHTTPMiddleware):
    """
    Reject requests with Content-Length exceeding the limit.
    Default: 110 MB (slightly above file upload max of 100 MB).
    """

    def __init__(self, app: ASGIApp, max_size_bytes: int = 115_343_360):
        super().__init__(app)
        self.max_size_bytes = max_size_bytes

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > self.max_size_bytes:
            logger.warning(
                "request_too_large",
                path=request.url.path,
                size=content_length,
                limit=self.max_size_bytes,
            )
            return Response(
                content='{"detail":"Request entity too large"}',
                status_code=413,
                media_type="application/json",
            )
        return await call_next(request)


# ── Request ID Middleware ────────────────────────────────

class RequestIdMiddleware(BaseHTTPMiddleware):
    """Inject a unique request ID into every request for audit trail."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        request_id = request.headers.get("X-Request-ID") or secrets.token_hex(8)
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response


# ── Request Latency Histogram ────────────────────────────
#
# Per-endpoint duration tracking exposed via the Prometheus metrics
# endpoint. Buckets are HDR-style log-scale so the same histogram covers
# fast LAN responses (≤5 ms) AND slow paths (uploads, federation RPC).
# Sampling is unconditional — the whole histogram lives in process
# memory at ~24 bytes per (endpoint, bucket) combo.
#
# Endpoint key: HTTP method + path-template (collapses /api/users/{id}
# variants so we don't explode the cardinality with one bucket per UUID).

_LATENCY_BUCKETS_MS = (
    1, 2, 5, 10, 25, 50, 100, 250, 500,
    1_000, 2_500, 5_000, 10_000, 30_000,
)


class _LatencyTracker:
    """Process-local request latency histogram. Thread-unsafe but the
    asyncio event loop serialises mutations on a single thread, which is
    enough for single-process Helen-Server deployments."""

    def __init__(self) -> None:
        # endpoint → list of bucket counts (one extra bucket = +Inf overflow)
        self._buckets: dict[str, list[int]] = {}
        self._totals: dict[str, int] = {}
        self._sum_ms: dict[str, float] = {}
        # Cap total endpoint cardinality so a misbehaving caller can't
        # OOM us with /api/x/<random> paths. The first 1000 endpoints
        # each get their own histogram; the rest share a "_other" bucket.
        self._max_endpoints = 1000

    def record(self, endpoint: str, duration_ms: float) -> None:
        if endpoint not in self._buckets:
            if len(self._buckets) >= self._max_endpoints:
                endpoint = "_other"
            if endpoint not in self._buckets:
                self._buckets[endpoint] = [0] * (len(_LATENCY_BUCKETS_MS) + 1)
                self._totals[endpoint] = 0
                self._sum_ms[endpoint] = 0.0
        # Find first bucket >= duration_ms
        idx = len(_LATENCY_BUCKETS_MS)  # default = +Inf bucket
        for i, b in enumerate(_LATENCY_BUCKETS_MS):
            if duration_ms <= b:
                idx = i
                break
        self._buckets[endpoint][idx] += 1
        self._totals[endpoint] += 1
        self._sum_ms[endpoint] += duration_ms

    def snapshot(self) -> dict[str, dict]:
        """Return per-endpoint stats — used by the Prometheus exposition."""
        out = {}
        for ep, buckets in self._buckets.items():
            total = self._totals[ep]
            sum_ms = self._sum_ms[ep]
            avg_ms = sum_ms / total if total > 0 else 0.0
            out[ep] = {
                "buckets": list(zip(_LATENCY_BUCKETS_MS + (float("inf"),), buckets)),
                "count": total,
                "sum_ms": sum_ms,
                "avg_ms": avg_ms,
            }
        return out

    def reset(self) -> None:
        self._buckets.clear()
        self._totals.clear()
        self._sum_ms.clear()


latency_tracker = _LatencyTracker()


def _normalize_endpoint(method: str, path: str) -> str:
    """Collapse path parameters to keep cardinality bounded.

    /api/users/abc123    →  /api/users/{id}
    /api/channels/x/messages  →  /api/channels/{id}/messages
    """
    # Strip query string.
    if "?" in path:
        path = path.split("?", 1)[0]
    # Replace UUID-like segments (32 hex chars) with {id}.
    parts = path.split("/")
    for i, p in enumerate(parts):
        if len(p) >= 8 and all(c in "0123456789abcdef" for c in p.lower()):
            parts[i] = "{id}"
        elif len(p) >= 32 and all(c.isalnum() or c in "-_" for c in p):
            # share_codes, JTI, etc.
            parts[i] = "{id}"
    return f"{method} {'/'.join(parts)}"


class RequestLatencyMiddleware(BaseHTTPMiddleware):
    """Tracks every request's wall-clock duration and records it into
    `latency_tracker`. The Prometheus endpoint reads this and emits
    standard Prometheus histogram metrics (helen_request_duration_ms).

    Adds an `X-Response-Time-Ms` header on the way out so client-side
    diagnostics can see the server-side share of the round-trip cost.
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        import time as _t
        start = _t.perf_counter()
        response = await call_next(request)
        duration_ms = (_t.perf_counter() - start) * 1000.0
        try:
            endpoint = _normalize_endpoint(request.method, request.url.path)
            latency_tracker.record(endpoint, duration_ms)
            response.headers["X-Response-Time-Ms"] = f"{duration_ms:.2f}"
        except Exception:
            pass  # never break a good response over instrumentation
        return response


# ── Login Rate Limiter ───────────────────────────────────
# IP-based sliding window for /api/auth/login and /api/auth/register

class LoginAttemptTracker:
    """
    Tracks failed login attempts per IP for brute-force protection.

    Limits:
      - 10 attempts per minute per IP
      - After 15 consecutive failures: 5-minute lockout for that IP
    """

    def __init__(
        self,
        max_attempts_per_minute: int = 10,
        lockout_threshold: int = 15,
        lockout_duration_sec: int = 300,
    ):
        self.max_attempts_per_minute = max_attempts_per_minute
        self.lockout_threshold = lockout_threshold
        self.lockout_duration_sec = lockout_duration_sec

        # ip → list of timestamps
        self._attempts: dict[str, list[float]] = defaultdict(list)
        # ip → (lock_until_timestamp, consecutive_failures)
        self._lockouts: dict[str, tuple[float, int]] = {}

    def is_locked(self, ip: str) -> bool:
        """Check if an IP is currently locked out."""
        if ip not in self._lockouts:
            return False
        lock_until, _ = self._lockouts[ip]
        if time.monotonic() > lock_until:
            del self._lockouts[ip]
            return False
        return True

    def check(self, ip: str) -> bool:
        """
        Check if a login attempt is allowed.
        Returns True if allowed, False if rate-limited.
        """
        now = time.monotonic()

        # Check lockout
        if self.is_locked(ip):
            return False

        # Sliding window: remove old attempts
        window_start = now - 60.0
        attempts = self._attempts[ip]
        self._attempts[ip] = [t for t in attempts if t > window_start]

        # Check rate
        if len(self._attempts[ip]) >= self.max_attempts_per_minute:
            return False

        return True

    def record_attempt(self, ip: str, success: bool) -> None:
        """Record a login attempt (success or failure)."""
        now = time.monotonic()

        if success:
            # Clear failure tracking on success
            self._attempts.pop(ip, None)
            self._lockouts.pop(ip, None)
            return

        # Record failure
        self._attempts[ip].append(now)

        # Check if we should lock this IP
        _, consecutive = self._lockouts.get(ip, (0, 0))
        consecutive += 1

        if consecutive >= self.lockout_threshold:
            self._lockouts[ip] = (
                now + self.lockout_duration_sec,
                consecutive,
            )
            logger.warning(
                "login_ip_locked",
                ip=ip,
                consecutive_failures=consecutive,
                lockout_seconds=self.lockout_duration_sec,
            )
        else:
            self._lockouts[ip] = (0, consecutive)

    def cleanup(self) -> None:
        """Remove stale entries (call periodically)."""
        now = time.monotonic()
        stale_ips = []
        for ip, attempts in self._attempts.items():
            if not attempts or (now - attempts[-1]) > 600:
                stale_ips.append(ip)
        for ip in stale_ips:
            del self._attempts[ip]
            self._lockouts.pop(ip, None)


# ── Global HTTP Rate Limiter ─────────────────────────────
# Token-bucket per-bucket-key with class-specific budgets. Protects every
# API endpoint, not just auth — an attacker with a valid token could
# otherwise hammer /api/messages, /api/admin, /api/files, …

class _TokenBucket:
    """Classic refilling bucket. `consume(n)` returns False when empty."""

    __slots__ = ("capacity", "refill_per_sec", "tokens", "last_ts")

    def __init__(self, capacity: float, refill_per_sec: float):
        self.capacity = capacity
        self.refill_per_sec = refill_per_sec
        self.tokens = capacity
        self.last_ts = time.monotonic()

    def consume(self, n: float = 1.0) -> tuple[bool, float]:
        """Return (allowed, retry_after_seconds). retry_after is 0 when allowed."""
        now = time.monotonic()
        dt = now - self.last_ts
        if dt > 0:
            self.tokens = min(self.capacity, self.tokens + dt * self.refill_per_sec)
            self.last_ts = now
        if self.tokens >= n:
            self.tokens -= n
            return True, 0.0
        # Deficit / refill_rate = seconds until the next token is available.
        deficit = n - self.tokens
        retry = deficit / self.refill_per_sec if self.refill_per_sec > 0 else 60.0
        return False, max(retry, 0.05)


# ── Path classification ─────────────────────────────────
# Each class has its own (capacity, refill-per-sec) budget. Upload is
# deliberately looser because large uploads span multiple chunk POSTs;
# admin is looser for operators; federation is tight because peers talk
# server-to-server and shouldn't spam.

_CLASS_AUTH = "auth"
_CLASS_UPLOAD = "upload"
_CLASS_FEDERATION = "federation"
_CLASS_ADMIN = "admin"
_CLASS_DEFAULT = "default"

# (capacity, refill_per_sec). Capacity = burst allowance, refill = steady state.
_DEFAULT_CLASS_BUDGETS: dict[str, tuple[float, float]] = {
    # Auth already has its own tracker, so this is the ceiling for the
    # *envelope* — keep it slightly above LoginAttemptTracker's 10/min.
    _CLASS_AUTH:       (30,  30 / 60),      # 30 burst, ~0.5/s
    _CLASS_UPLOAD:     (120, 120 / 60),     # 120 burst, 2/s
    _CLASS_FEDERATION: (60,  60 / 60),      # 60 burst, 1/s
    _CLASS_ADMIN:      (300, 300 / 60),     # 300 burst, 5/s
    _CLASS_DEFAULT:    (240, 240 / 60),     # 240 burst, 4/s — 14 400/hr/user
}


def _classify_path(path: str) -> str:
    if path.startswith("/api/auth"):
        return _CLASS_AUTH
    if path.startswith("/api/federation"):
        return _CLASS_FEDERATION
    if path.startswith("/api/admin"):
        return _CLASS_ADMIN
    if (
        path.startswith("/api/files")
        or path.startswith("/api/uploads")
        or path.startswith("/api/upload-session")
        or path.startswith("/api/ingest")
    ):
        return _CLASS_UPLOAD
    return _CLASS_DEFAULT


# Paths that should bypass the limiter entirely. Health probes must
# never 429 (tools like Helen-Admin's watchdog poll every few seconds),
# and WebSockets shouldn't count against per-request budgets.
_BYPASS_PATH_PREFIXES = (
    "/api/health",
    "/api/ping",
    "/ws",
    "/socket.io",
    "/static",
    "/docs",
    "/redoc",
    "/openapi.json",
)


def _client_ip(request: "Request") -> str:
    # Trust X-Forwarded-For only when present — typical LAN deployments don't
    # use a proxy, so request.client.host is authoritative. If behind a reverse
    # proxy later, the operator can strip/set this header there.
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    client = request.client
    return client.host if client else "unknown"


def _is_lan_or_loopback(ip: str) -> bool:
    """Best-effort: treat 127/8, 10/8, 172.16/12, 192.168/16, ::1 as trusted."""
    if ip in ("127.0.0.1", "::1", "localhost"):
        return True
    if ip.startswith("10.") or ip.startswith("192.168."):
        return True
    if ip.startswith("172."):
        try:
            second = int(ip.split(".", 2)[1])
            return 16 <= second <= 31
        except (ValueError, IndexError):
            return False
    return False


class GlobalRateLimiter:
    """Bucket store keyed by (class, identity). Thread-safe enough for the
    single-process FastAPI worker; horizontal scale would need Redis."""

    def __init__(self, class_budgets: dict[str, tuple[float, float]] | None = None):
        self._budgets = class_budgets or _DEFAULT_CLASS_BUDGETS
        self._buckets: dict[tuple[str, str], _TokenBucket] = {}

    def check(self, cls: str, identity: str) -> tuple[bool, float]:
        key = (cls, identity)
        bucket = self._buckets.get(key)
        if bucket is None:
            cap, refill = self._budgets.get(cls, self._budgets[_CLASS_DEFAULT])
            bucket = _TokenBucket(cap, refill)
            self._buckets[key] = bucket
        return bucket.consume(1.0)

    def size(self) -> int:
        return len(self._buckets)

    def cleanup(self, now: float | None = None) -> int:
        """Drop buckets that have been full for 10 minutes (no active client)."""
        now = now if now is not None else time.monotonic()
        stale = [
            k for k, b in self._buckets.items()
            if b.tokens >= b.capacity and (now - b.last_ts) > 600
        ]
        for k in stale:
            self._buckets.pop(k, None)
        return len(stale)


# Process-wide singleton used by the middleware below.
global_rate_limiter = GlobalRateLimiter()


class GlobalRateLimitMiddleware(BaseHTTPMiddleware):
    """
    Apply the rate limit to every HTTP request.

    Identity resolution:
      1. If the request has a valid JWT, key on `user_id` (per-user budget).
      2. Otherwise key on client IP (per-IP budget for anonymous traffic).

    Bypass:
      * LAN/loopback traffic is pass-through (toggle via `trust_lan=False`
        for production deployments that sit behind a public edge).
      * Health probes + WebSockets always bypass.

    On deny:
      * Returns 429 with `Retry-After` (seconds) and a JSON body.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        limiter: GlobalRateLimiter | None = None,
        trust_lan: bool = True,
        enabled: bool = True,
    ):
        super().__init__(app)
        self._limiter = limiter or global_rate_limiter
        self._trust_lan = trust_lan
        self._enabled = enabled

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if not self._enabled:
            return await call_next(request)

        path = request.url.path
        if any(path.startswith(p) for p in _BYPASS_PATH_PREFIXES):
            return await call_next(request)

        ip = _client_ip(request)
        if self._trust_lan and _is_lan_or_loopback(ip):
            return await call_next(request)

        # Prefer authenticated identity so different users on the same NAT
        # don't share a bucket. Parsing the JWT here is cheap (HS256 + small
        # payload) and lets us key on a stable user_id.
        identity = f"ip:{ip}"
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
            try:
                from app.core.security import decode_token_no_http
                payload = decode_token_no_http(token)
                if payload and payload.get("sub"):
                    identity = f"user:{payload['sub']}"
            except Exception:
                # Malformed header — fall through to IP-based keying.
                pass

        cls = _classify_path(path)
        allowed, retry_after = self._limiter.check(cls, identity)
        if not allowed:
            # Surface minimal state in logs so operators can spot abuse.
            logger.warning(
                "rate_limited",
                path=path,
                cls=cls,
                identity=identity,
                retry_after=round(retry_after, 2),
            )
            cap, refill = self._limiter._budgets.get(cls, self._limiter._budgets[_CLASS_DEFAULT])
            return Response(
                content=(
                    '{"detail":"Too many requests",'
                    f'"retry_after":{round(retry_after, 2)}}}'
                ),
                status_code=429,
                media_type="application/json",
                headers={
                    "Retry-After": str(max(1, int(retry_after + 0.99))),
                    "X-RateLimit-Limit": str(int(cap)),
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": str(int(time.time() + retry_after)),
                    "X-RateLimit-Class": cls,
                },
            )

        # Surface the live bucket state on every successful response too,
        # so good clients can pace themselves before they hit a 429. This
        # is the same convention GitHub / Stripe use.
        response = await call_next(request)
        try:
            bucket = self._limiter._buckets.get((cls, identity))
            if bucket is not None:
                cap, _ = self._limiter._budgets.get(cls, self._limiter._budgets[_CLASS_DEFAULT])
                response.headers["X-RateLimit-Limit"] = str(int(cap))
                response.headers["X-RateLimit-Remaining"] = str(int(max(0, bucket.tokens)))
                response.headers["X-RateLimit-Class"] = cls
        except Exception:
            pass  # never fail a successful response over a header
        return response


# Global instance
login_tracker = LoginAttemptTracker()


# ── Account Lockout (per-username) ───────────────────────

class AccountLockoutTracker:
    """
    Tracks failed login attempts per username for account lockout.
    After 10 failures, locks account for 15 minutes.
    """

    def __init__(
        self,
        max_failures: int = 10,
        lockout_duration_sec: int = 900,
    ):
        self.max_failures = max_failures
        self.lockout_duration_sec = lockout_duration_sec
        self._failures: dict[str, tuple[int, float]] = {}  # username → (count, last_failure_time)

    def is_locked(self, username: str) -> bool:
        if username not in self._failures:
            return False
        count, last_time = self._failures[username]
        if count < self.max_failures:
            return False
        if time.monotonic() - last_time > self.lockout_duration_sec:
            del self._failures[username]
            return False
        return True

    def record_failure(self, username: str) -> None:
        count, _ = self._failures.get(username, (0, 0))
        self._failures[username] = (count + 1, time.monotonic())

    def record_success(self, username: str) -> None:
        self._failures.pop(username, None)


account_lockout = AccountLockoutTracker()
