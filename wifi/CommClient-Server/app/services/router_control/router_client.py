"""
HelenRouterClient — async HTTP client for Helen-Router admin API.

Wraps a single shared ``httpx.AsyncClient`` with:
  * Connection pooling (keepalive across requests).
  * Per-call timeout overrides.
  * Bearer token injection from :class:`RouterConfigStore`.
  * Exponential-backoff retry for **transient** failures
    (``ConnectError``, ``ReadTimeout``, 502/503/504). Idempotent
    methods retry by default; non-idempotent ones (POST/PATCH)
    only retry if the caller explicitly opts in.
  * Lightweight reachability probe (``is_reachable``) used by the
    admin overview to render a green/red light.
  * Uniform :class:`RouterResponse` return — never raises for HTTP
    failures, only for true network errors that the FastAPI layer
    should surface as 502.

The client is process-singleton (see :func:`get_router_client`) so
the keepalive pool is shared across all admin API calls.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Mapping, Optional

import httpx

from app.core.logging import get_logger
from app.services.router_control.config_store import (
    RouterConfigStore,
    get_router_config_store,
)

logger = get_logger(__name__)


# ── Tunables ──────────────────────────────────────────────────────


DEFAULT_TIMEOUT = httpx.Timeout(15.0, connect=3.0)
"""Per-request timeout. Connect timeout is aggressive (3 s) because
Helen-Router runs on the same LAN — anything slower is a fault."""

MAX_KEEPALIVE_CONNECTIONS = 32
MAX_CONNECTIONS = 64

MAX_RETRIES = 3
"""Cap on retry attempts. Final attempt is *not* counted as a retry
— a value of 3 means we try 1 + 3 = 4 times total."""

INITIAL_BACKOFF_SECONDS = 0.2
BACKOFF_FACTOR = 2.0
MAX_BACKOFF_SECONDS = 2.5
"""Exponential backoff cap. Total worst-case wait for 3 retries with
these settings: 0.2 + 0.4 + 0.8 = 1.4 s — keeps a 502-on-router
from making the admin UI feel hung."""

# HTTP status codes that should trigger a retry — server-side
# overload or routing hiccup, not a permanent failure.
RETRIABLE_STATUS_CODES: frozenset[int] = frozenset({502, 503, 504})

# httpx exceptions that indicate transient network conditions.
RETRIABLE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.RemoteProtocolError,
)


# ── Error types ──────────────────────────────────────────────────


class RouterUnreachableError(Exception):
    """Raised after all retries are exhausted.

    The FastAPI proxy layer turns this into a 502 with the error
    string in the body so the admin UI can render a helpful
    "router offline" panel.
    """

    def __init__(self, message: str, *, last_status: int | None = None,
                 attempts: int = 0):
        super().__init__(message)
        self.last_status = last_status
        self.attempts = attempts


# ── Response envelope ────────────────────────────────────────────


@dataclass
class RouterResponse:  # noqa: D101 — described above
    """Uniform return value for every router-client call.

    Why not just return ``httpx.Response``? Two reasons:

      * The proxy handler needs to read the body anyway (to log
        latency + audit), so eager-loading once and shipping
        bytes around is cheaper than juggling httpx's
        stream lifecycle.
      * We want to expose ``error`` as a structured field so
        callers can differentiate transport failures (502
        bubbling up) from upstream HTTP errors (4xx/5xx the
        router itself returned).
    """

    status: int
    headers: dict[str, str]
    body: bytes
    error: Optional[str] = None
    elapsed_ms: float = 0.0
    attempts: int = 1
    upstream_url: Optional[str] = None

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "")

    def json(self) -> Any:
        """Decode JSON body or raise ``ValueError`` if not JSON."""
        import json as _json
        return _json.loads(self.body.decode("utf-8"))

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


# ── The client ───────────────────────────────────────────────────


class HelenRouterClient:
    """Async, retrying HTTP client for the Helen-Router admin API.

    Use :func:`get_router_client` to access the singleton. The
    singleton lazily creates an ``httpx.AsyncClient`` on first
    use; call :meth:`aclose` from your shutdown hook.
    """

    def __init__(
        self,
        config_store: Optional[RouterConfigStore] = None,
        *,
        timeout: httpx.Timeout = DEFAULT_TIMEOUT,
        max_retries: int = MAX_RETRIES,
    ) -> None:
        self._config = config_store or get_router_config_store()
        self._timeout = timeout
        self._max_retries = max_retries
        self._client: Optional[httpx.AsyncClient] = None
        self._client_lock = asyncio.Lock()

    # ── Lifecycle ───────────────────────────────────────────────

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is not None and not self._client.is_closed:
            return self._client
        async with self._client_lock:
            if self._client is None or self._client.is_closed:
                self._client = httpx.AsyncClient(
                    timeout=self._timeout,
                    limits=httpx.Limits(
                        max_keepalive_connections=MAX_KEEPALIVE_CONNECTIONS,
                        max_connections=MAX_CONNECTIONS,
                    ),
                    follow_redirects=False,
                    headers={
                        "User-Agent": "Helen-Server-RouterProxy/1.0",
                    },
                )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    # ── Reachability ───────────────────────────────────────────

    async def is_reachable(self, *, timeout: float = 2.0) -> bool:
        """Cheap GET to ``/router/health`` — used by the UI's
        green/red status pill. Never raises; returns ``False``
        if we can't reach the router for any reason.
        """
        try:
            resp = await self._request_once(
                "GET", "/router/health",
                params=None, json=None, content=None,
                extra_headers=None,
                timeout=httpx.Timeout(timeout, connect=timeout),
            )
            return 200 <= resp.status_code < 300
        except Exception:
            return False

    # ── HTTP verb helpers (typed convenience) ───────────────────

    async def get(
        self, path: str, *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        retry: bool = True,
    ) -> RouterResponse:
        return await self.request(
            "GET", path,
            params=params, headers=headers, retry=retry,
        )

    async def post(
        self, path: str, *,
        json: Any = None,
        content: bytes | None = None,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        retry: bool = False,
    ) -> RouterResponse:
        return await self.request(
            "POST", path,
            json=json, content=content, params=params,
            headers=headers, retry=retry,
        )

    async def put(
        self, path: str, *,
        json: Any = None,
        content: bytes | None = None,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        retry: bool = True,
    ) -> RouterResponse:
        return await self.request(
            "PUT", path,
            json=json, content=content, params=params,
            headers=headers, retry=retry,
        )

    async def delete(
        self, path: str, *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        retry: bool = True,
    ) -> RouterResponse:
        return await self.request(
            "DELETE", path,
            params=params, headers=headers, retry=retry,
        )

    async def patch(
        self, path: str, *,
        json: Any = None,
        content: bytes | None = None,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        retry: bool = False,
    ) -> RouterResponse:
        return await self.request(
            "PATCH", path,
            json=json, content=content, params=params,
            headers=headers, retry=retry,
        )

    # ── Streaming variant ───────────────────────────────────────

    async def stream(
        self, method: str, path: str, *,
        params: Mapping[str, Any] | None = None,
        content: bytes | None = None,
        json: Any = None,
        headers: Mapping[str, str] | None = None,
    ) -> AsyncIterator[tuple[int, dict[str, str], AsyncIterator[bytes]]]:
        """Streaming context manager — yields ``(status, headers, chunks)``.

        Used by the proxy handler for endpoints that may produce
        large responses (logs, exports). Always wrap in
        ``async with``::

            async with client.stream("GET", "/router/dns/log") as (
                status, headers, chunks,
            ):
                async for chunk in chunks:
                    await response.write(chunk)
        """
        # Implemented as a thin generator because ``async with`` on
        # ``httpx.AsyncClient.stream`` is the right abstraction.
        client = await self._ensure_client()
        cfg = await self._config.get()
        target = self._build_url(cfg.base_url, path)
        out_headers = await self._merged_headers(headers, cfg.token)

        async with client.stream(
            method, target,
            params=dict(params) if params else None,
            content=content,
            json=json,
            headers=out_headers,
        ) as r:
            async def _chunks() -> AsyncIterator[bytes]:
                async for chunk in r.aiter_raw():
                    yield chunk

            yield r.status_code, dict(r.headers), _chunks()

    # ── Generic request with retry ──────────────────────────────

    async def request(
        self, method: str, path: str, *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        content: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        retry: bool = True,
        timeout: httpx.Timeout | None = None,
    ) -> RouterResponse:
        """Make a request with retry policy.

        Raises:
            RouterUnreachableError: every attempt failed transiently.
        """
        cfg = await self._config.get()
        merged_headers = await self._merged_headers(headers, cfg.token)

        max_attempts = self._max_retries + 1 if retry else 1
        backoff = INITIAL_BACKOFF_SECONDS
        last_exc: BaseException | None = None
        last_status: int | None = None
        started = time.perf_counter()

        for attempt in range(1, max_attempts + 1):
            try:
                resp = await self._request_once(
                    method, path,
                    params=params, json=json, content=content,
                    extra_headers=merged_headers,
                    timeout=timeout,
                )
            except RETRIABLE_EXCEPTIONS as exc:
                last_exc = exc
                last_status = None
                logger.warning(
                    "router_client_transient_error",
                    method=method, path=path, attempt=attempt,
                    error=str(exc),
                )
                if attempt >= max_attempts:
                    break
                await asyncio.sleep(min(backoff, MAX_BACKOFF_SECONDS))
                backoff *= BACKOFF_FACTOR
                continue
            except httpx.RequestError as exc:
                # Non-retriable network error (invalid URL, etc.)
                elapsed_ms = (time.perf_counter() - started) * 1000
                raise RouterUnreachableError(
                    f"router request failed: {exc}",
                    attempts=attempt,
                ) from exc

            if resp.status_code in RETRIABLE_STATUS_CODES and retry \
                    and attempt < max_attempts:
                last_status = resp.status_code
                logger.warning(
                    "router_client_retriable_status",
                    method=method, path=path, attempt=attempt,
                    status=resp.status_code,
                )
                await asyncio.sleep(min(backoff, MAX_BACKOFF_SECONDS))
                backoff *= BACKOFF_FACTOR
                continue

            elapsed_ms = (time.perf_counter() - started) * 1000
            return RouterResponse(
                status=resp.status_code,
                headers=dict(resp.headers),
                body=resp.content,
                elapsed_ms=elapsed_ms,
                attempts=attempt,
                upstream_url=str(resp.request.url),
            )

        # All attempts exhausted
        raise RouterUnreachableError(
            f"router unreachable after {max_attempts} attempt(s): "
            f"{last_exc!r} (last_status={last_status})",
            last_status=last_status,
            attempts=max_attempts,
        )

    # ── Single attempt (no retry) ───────────────────────────────

    async def _request_once(
        self, method: str, path: str, *,
        params: Mapping[str, Any] | None,
        json: Any,
        content: bytes | None,
        extra_headers: Mapping[str, str] | None,
        timeout: httpx.Timeout | None,
    ) -> httpx.Response:
        client = await self._ensure_client()
        cfg = await self._config.get()
        target = self._build_url(cfg.base_url, path)

        # If caller didn't pre-merge token (e.g. is_reachable), do it.
        if extra_headers is None:
            extra_headers = await self._merged_headers(None, cfg.token)

        return await client.request(
            method.upper(),
            target,
            params=dict(params) if params else None,
            content=content,
            json=json,
            headers=dict(extra_headers),
            timeout=timeout or self._timeout,
        )

    # ── Header construction ─────────────────────────────────────

    async def _merged_headers(
        self,
        caller_headers: Mapping[str, str] | None,
        token: str,
    ) -> dict[str, str]:
        """Build the outgoing header set.

        Strips any client-supplied ``Authorization`` (the proxy
        layer must not let an admin's JWT leak to the router) and
        replaces it with ``Bearer <ROUTER_TOKEN>``. Also strips
        hop-by-hop headers that should never traverse the proxy.
        """
        out: dict[str, str] = {}
        dropped = self._HOP_BY_HOP | {
            "authorization", "cookie", "host", "content-length",
        }
        if caller_headers:
            for k, v in caller_headers.items():
                if k.lower() in dropped:
                    continue
                out[k] = v
        if token:
            out["Authorization"] = f"Bearer {token}"
        out.setdefault("Accept", "application/json")
        return out

    @staticmethod
    def _build_url(base_url: str, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return base_url.rstrip("/") + path

    # Hop-by-hop headers per RFC 7230 §6.1.
    _HOP_BY_HOP: frozenset[str] = frozenset({
        "connection", "keep-alive", "proxy-authenticate",
        "proxy-authorization", "te", "trailers",
        "transfer-encoding", "upgrade",
    })


# ── Singleton accessor ───────────────────────────────────────────


_client: Optional[HelenRouterClient] = None
_client_lock = asyncio.Lock()


async def get_router_client() -> HelenRouterClient:
    """Return the process-wide :class:`HelenRouterClient`."""
    global _client
    if _client is not None:
        return _client
    async with _client_lock:
        if _client is None:
            _client = HelenRouterClient()
        return _client
