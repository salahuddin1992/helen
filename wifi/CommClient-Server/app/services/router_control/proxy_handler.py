"""
RouterProxyHandler — generic FastAPI request forwarder.

Responsibilities
----------------
  * Read the incoming :class:`fastapi.Request` (headers, body,
    query string).
  * Strip / rewrite headers for the outbound leg — replace any
    inbound ``Authorization`` with the router's shared token,
    drop hop-by-hop headers, preserve correlation IDs.
  * Forward to Helen-Router via :class:`HelenRouterClient`.
  * Stream the response back to the caller. For small JSON
    payloads we just buffer; for unbounded responses (logs,
    DNS dumps) we stream raw chunks.
  * Log latency + outcome via ``structlog``.
  * Emit audit entries for write operations
    (POST/PUT/PATCH/DELETE) via :class:`RouterAuditHook`.

The handler is stateless beyond the injected client + audit hook,
so a single instance can be shared across the whole router-control
APIRouter.
"""

from __future__ import annotations

import time
from typing import Any, Mapping, Optional

from fastapi import Request, status
from fastapi.responses import JSONResponse, Response, StreamingResponse

from app.core.logging import get_logger
from app.services.router_control.audit_hook import RouterAuditHook
from app.services.router_control.router_client import (
    HelenRouterClient,
    RouterResponse,
    RouterUnreachableError,
)

logger = get_logger(__name__)


# ── Request-header policy ────────────────────────────────────────

# Headers we deliberately strip on the way out — the router does
# not (and must not) trust anything from the calling admin's
# browser. The shared bearer token is the only auth the router
# needs to see.
_STRIP_REQUEST_HEADERS: frozenset[str] = frozenset({
    "authorization", "cookie", "host", "content-length",
    "connection", "keep-alive", "proxy-authenticate",
    "proxy-authorization", "te", "trailers",
    "transfer-encoding", "upgrade",
    # Anything Helen-Server adds internally — re-add on the next
    # hop only if explicitly requested.
    "x-forwarded-by", "x-forwarded-for", "x-forwarded-proto",
    "x-forwarded-host",
})

# Headers we *keep* by default (whitelist is shorter than blacklist
# for the forward direction because admin browsers may add quirky
# ones — see CORS preflight). These survive into the outbound call.
_PRESERVE_REQUEST_HEADERS: frozenset[str] = frozenset({
    "accept", "accept-encoding", "accept-language",
    "content-type", "x-request-id", "x-correlation-id",
})

# Response headers we never relay back to the admin browser.
_STRIP_RESPONSE_HEADERS: frozenset[str] = frozenset({
    "connection", "keep-alive", "proxy-authenticate",
    "proxy-authorization", "te", "trailers",
    "transfer-encoding", "upgrade", "content-length",
})


# ── Tunables ─────────────────────────────────────────────────────

STREAM_THRESHOLD_BYTES = 1024 * 1024
"""If the upstream Content-Length is bigger than this we switch from
buffered to streaming. Avoids buffering multi-MB log dumps."""


# ── Proxy handler ────────────────────────────────────────────────


class RouterProxyHandler:
    """Stateless forwarder. Inject the client + audit hook once."""

    def __init__(
        self,
        client: HelenRouterClient,
        audit: Optional[RouterAuditHook] = None,
    ) -> None:
        self._client = client
        self._audit = audit or RouterAuditHook()

    # ── Main entry point ────────────────────────────────────────

    async def forward(
        self,
        request: Request,
        target_path: str,
        *,
        admin_user_id: str,
        retry: Optional[bool] = None,
        audit_event: Optional[str] = None,
        stream: bool = False,
    ) -> Response:
        """Forward an incoming admin request to Helen-Router.

        Args:
            request:        Starlette/FastAPI Request.
            target_path:    Path on Helen-Router (must start with /).
            admin_user_id:  The admin's user id — used for audit.
            retry:          Override the default retry policy.
                            ``None`` → idempotent verbs retry,
                            unsafe verbs don't.
            audit_event:    If provided, emit an audit entry under
                            this event name regardless of HTTP verb.
                            Default: auto-derive for write verbs.
            stream:         Force streaming response back. Otherwise
                            we auto-detect by content-length.
        """
        method = request.method.upper()
        body = await request.body() if method not in ("GET", "HEAD") else None
        fwd_headers = self._sanitize_request_headers(request.headers)
        query_params = dict(request.query_params)

        is_write = method in ("POST", "PUT", "PATCH", "DELETE")
        effective_retry = (
            retry if retry is not None
            else method in ("GET", "HEAD", "PUT", "DELETE")
        )
        # We retry DELETE because the router endpoints are idempotent —
        # deleting an already-removed neighbour returns 404 not 500, so
        # a network blip on the first attempt is safe to retry.

        # ── Audit (pre-write hook captures intent) ──────────────
        # We log the intent BEFORE the call so a hung router is
        # still visible in the audit chain. The post-call hook
        # then updates with the outcome.
        audit_token: Optional[str] = None
        if is_write or audit_event:
            audit_token = await self._audit.before(
                event=audit_event or f"router.{method.lower()}",
                user_id=admin_user_id,
                method=method,
                path=target_path,
                query=query_params,
                body=body,
                client_ip=self._client_ip(request),
            )

        started = time.perf_counter()
        try:
            if stream:
                return await self._forward_streaming(
                    method, target_path,
                    params=query_params, content=body,
                    headers=fwd_headers,
                    retry=effective_retry,
                    audit_token=audit_token, is_write=is_write,
                    started=started, admin_user_id=admin_user_id,
                )
            resp = await self._client.request(
                method, target_path,
                params=query_params,
                content=body,
                headers=fwd_headers,
                retry=effective_retry,
            )
        except RouterUnreachableError as exc:
            logger.error(
                "router_proxy_unreachable",
                method=method, path=target_path,
                error=str(exc), attempts=exc.attempts,
                admin_user_id=admin_user_id,
            )
            if audit_token:
                await self._audit.after(
                    audit_token,
                    success=False,
                    status_code=502,
                    error=str(exc),
                )
            return JSONResponse(
                {
                    "error": "router_unreachable",
                    "detail": str(exc),
                    "attempts": exc.attempts,
                    "last_status": exc.last_status,
                },
                status_code=status.HTTP_502_BAD_GATEWAY,
            )

        elapsed_ms = (time.perf_counter() - started) * 1000
        logger.info(
            "router_proxy_call",
            method=method, path=target_path,
            status=resp.status,
            elapsed_ms=round(elapsed_ms, 2),
            attempts=resp.attempts,
            admin_user_id=admin_user_id,
        )

        if audit_token:
            await self._audit.after(
                audit_token,
                success=resp.ok,
                status_code=resp.status,
                elapsed_ms=elapsed_ms,
            )

        return self._build_response(resp, force_stream=stream)

    # ── Streaming path ──────────────────────────────────────────

    async def _forward_streaming(
        self,
        method: str, target_path: str, *,
        params: Mapping[str, Any] | None,
        content: bytes | None,
        headers: Mapping[str, str],
        retry: bool,
        audit_token: Optional[str],
        is_write: bool,
        started: float,
        admin_user_id: str,
    ) -> Response:
        """For endpoints that may produce large responses.

        We send the request with ``stream=True`` (httpx semantics)
        so the response object is available BEFORE the body has
        been drained. That lets us inspect the status code and
        headers, then pipe the raw chunks straight back through
        ``StreamingResponse`` — zero buffering at the proxy.
        """
        cli = await self._client._ensure_client()  # noqa: SLF001
        cfg = await self._client._config.get()     # noqa: SLF001
        url = cfg.base_url.rstrip("/") + target_path
        merged = await self._client._merged_headers(  # noqa: SLF001
            headers, cfg.token,
        )

        import httpx
        req = cli.build_request(
            method, url,
            params=dict(params) if params else None,
            content=content,
            headers=merged,
        )
        try:
            r = await cli.send(req, stream=True)
        except httpx.RequestError as exc:
            logger.error("router_proxy_stream_error",
                         method=method, path=target_path,
                         error=str(exc))
            if audit_token:
                await self._audit.after(
                    audit_token, success=False, status_code=502,
                    error=str(exc),
                )
            return JSONResponse(
                {"error": "router_unreachable", "detail": str(exc)},
                status_code=502,
            )

        out_headers = {
            k: v for k, v in r.headers.items()
            if k.lower() not in _STRIP_RESPONSE_HEADERS
        }

        if audit_token:
            await self._audit.after(
                audit_token,
                success=200 <= r.status_code < 300,
                status_code=r.status_code,
                elapsed_ms=(time.perf_counter() - started) * 1000,
            )

        async def _relay():
            try:
                async for chunk in r.aiter_raw():
                    yield chunk
            finally:
                await r.aclose()

        return StreamingResponse(
            _relay(),
            status_code=r.status_code,
            headers=out_headers,
            media_type=r.headers.get("content-type"),
        )

    # ── Response shaping ───────────────────────────────────────

    def _build_response(
        self, rr: RouterResponse, *, force_stream: bool = False,
    ) -> Response:
        out_headers = {
            k: v for k, v in rr.headers.items()
            if k.lower() not in _STRIP_RESPONSE_HEADERS
        }
        out_headers["X-Router-Latency-Ms"] = f"{rr.elapsed_ms:.1f}"
        out_headers["X-Router-Attempts"] = str(rr.attempts)

        # Auto-stream if the body is huge AND the caller didn't
        # ask for the buffered representation.
        if (force_stream or len(rr.body) > STREAM_THRESHOLD_BYTES):
            async def _chunked():
                # Yield in 64 KiB pieces — keeps memory profile flat
                # for very large bodies that we already buffered.
                chunk_size = 64 * 1024
                for i in range(0, len(rr.body), chunk_size):
                    yield rr.body[i:i + chunk_size]
            return StreamingResponse(
                _chunked(),
                status_code=rr.status,
                headers=out_headers,
                media_type=rr.content_type or None,
            )

        return Response(
            content=rr.body,
            status_code=rr.status,
            headers=out_headers,
            media_type=rr.content_type or None,
        )

    # ── Helpers ────────────────────────────────────────────────

    @staticmethod
    def _sanitize_request_headers(
        in_headers: Mapping[str, str],
    ) -> dict[str, str]:
        """Drop auth/hop-by-hop, preserve a small whitelist."""
        out: dict[str, str] = {}
        for k, v in in_headers.items():
            kl = k.lower()
            if kl in _STRIP_REQUEST_HEADERS:
                continue
            if kl in _PRESERVE_REQUEST_HEADERS or kl.startswith("x-"):
                out[k] = v
        return out

    @staticmethod
    def _client_ip(request: Request) -> str:
        if request.client:
            return request.client.host
        # Honour an upstream X-Forwarded-For if a load balancer
        # sits in front of Helen-Server (rare on LAN, common in
        # cloud deployments).
        xff = request.headers.get("x-forwarded-for", "")
        return xff.split(",")[0].strip() if xff else "unknown"
