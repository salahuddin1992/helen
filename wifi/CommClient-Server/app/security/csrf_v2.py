"""
Phase 6 / Module AE — Double-submit-cookie CSRF middleware (v2).

* Issues a ``helen_csrf`` cookie on safe responses (SameSite=Strict).
* Requires the same value to appear in the ``X-CSRF-Token`` header for
  every state-changing request (POST/PUT/PATCH/DELETE).
* Also validates ``Origin`` / ``Referer`` against an allow-list.
* Endpoints using bearer-token (Authorization: Bearer …) are exempt
  because they're inherently CSRF-immune for API clients.
"""
from __future__ import annotations

import hmac
import os
import secrets
from dataclasses import dataclass, field
from typing import Iterable, Optional
from urllib.parse import urlparse

from starlette.types import ASGIApp, Message, Receive, Scope, Send


SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


@dataclass
class CSRFConfig:
    cookie_name: str = "helen_csrf"
    header_name: str = "x-csrf-token"
    cookie_max_age: int = 3600 * 24 * 7
    same_site: str = "Strict"
    secure: bool = True
    httponly: bool = False           # JS needs to read the value
    allowed_origins: list[str] = field(default_factory=list)
    exempt_prefixes: list[str] = field(default_factory=lambda: [
        "/api/health", "/metrics",
        "/api/auth/refresh",   # uses bearer + own anti-replay
    ])


class CSRFv2:
    def __init__(self, app: ASGIApp, config: Optional[CSRFConfig] = None) -> None:
        self.app = app
        self.cfg = config or CSRFConfig()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "GET").upper()
        path = scope.get("path", "") or ""
        headers = {
            k.decode("latin-1", "ignore").lower(): v.decode("latin-1", "ignore")
            for k, v in (scope.get("headers") or [])
        }

        if any(path.startswith(p) for p in self.cfg.exempt_prefixes):
            await self._with_cookie(scope, receive, send, headers)
            return

        if method in SAFE_METHODS:
            await self._with_cookie(scope, receive, send, headers)
            return

        # bearer-token auth exempts CSRF
        if headers.get("authorization", "").lower().startswith("bearer "):
            await self.app(scope, receive, send)
            return

        # Validate Origin / Referer
        origin = headers.get("origin") or self._origin_of(headers.get("referer"))
        if self.cfg.allowed_origins and origin and origin not in self.cfg.allowed_origins:
            await self._respond_403(send, "origin not allowed")
            return

        # Double-submit cookie check
        cookie_value = self._read_cookie(headers.get("cookie", ""), self.cfg.cookie_name)
        sent_token = headers.get(self.cfg.header_name)
        if not cookie_value or not sent_token or not hmac.compare_digest(cookie_value, sent_token):
            await self._respond_403(send, "missing/invalid CSRF token")
            return

        await self.app(scope, receive, send)

    # ── helpers ─────────────────────────────────────────────

    @staticmethod
    def _origin_of(referer: Optional[str]) -> Optional[str]:
        if not referer:
            return None
        try:
            p = urlparse(referer)
            return f"{p.scheme}://{p.netloc}" if p.scheme else None
        except Exception:                                           # pragma: no cover
            return None

    @staticmethod
    def _read_cookie(cookie_header: str, name: str) -> Optional[str]:
        if not cookie_header:
            return None
        for entry in cookie_header.split(";"):
            entry = entry.strip()
            if entry.startswith(name + "="):
                return entry.split("=", 1)[1]
        return None

    async def _with_cookie(
        self, scope: Scope, receive: Receive, send: Send,
        headers: dict[str, str],
    ) -> None:
        if self._read_cookie(headers.get("cookie", ""), self.cfg.cookie_name):
            await self.app(scope, receive, send)
            return
        new_token = secrets.token_urlsafe(32)
        cookie_value = (
            f"{self.cfg.cookie_name}={new_token}; "
            f"Max-Age={self.cfg.cookie_max_age}; "
            f"Path=/; SameSite={self.cfg.same_site}"
            + ("; Secure" if self.cfg.secure else "")
            + ("; HttpOnly" if self.cfg.httponly else "")
        )

        async def _send(message: Message) -> None:
            if message["type"] == "http.response.start":
                hdrs = list(message.get("headers") or [])
                hdrs.append((b"set-cookie", cookie_value.encode("latin-1", "ignore")))
                message = {**message, "headers": hdrs}
            await send(message)

        await self.app(scope, receive, _send)

    async def _respond_403(self, send: Send, reason: str) -> None:
        import json
        body = json.dumps({"detail": "csrf", "reason": reason}).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": 403,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        })
        await send({"type": "http.response.body", "body": body})


def attach_csrf_v2(app, config: Optional[CSRFConfig] = None) -> CSRFv2:
    return CSRFv2(app, config)
