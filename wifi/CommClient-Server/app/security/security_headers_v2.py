"""
Phase 6 / Module AE — Comprehensive security headers middleware.

Adds:
    * Content-Security-Policy (strict + per-request nonce)
    * Strict-Transport-Security
    * X-Content-Type-Options
    * X-Frame-Options
    * Referrer-Policy
    * Permissions-Policy
    * Cross-Origin-Embedder-Policy
    * Cross-Origin-Opener-Policy
    * Cross-Origin-Resource-Policy
    * X-Permitted-Cross-Domain-Policies
    * Server: helen

The nonce is exposed as ``scope["state"]["csp_nonce"]`` so HTML
responses (admin pages) can stamp it into inline ``<script nonce="…">``.
"""
from __future__ import annotations

import base64
import os
import secrets
from dataclasses import dataclass, field
from typing import Iterable, Optional

from starlette.types import ASGIApp, Message, Receive, Scope, Send


DEFAULT_PERMISSIONS = ", ".join([
    "accelerometer=()",
    "ambient-light-sensor=()",
    "autoplay=(self)",
    "battery=()",
    "camera=(self)",
    "clipboard-read=(self)",
    "clipboard-write=(self)",
    "display-capture=(self)",
    "fullscreen=(self)",
    "geolocation=()",
    "gyroscope=()",
    "magnetometer=()",
    "microphone=(self)",
    "midi=()",
    "payment=()",
    "publickey-credentials-get=(self)",
    "screen-wake-lock=(self)",
    "usb=()",
    "xr-spatial-tracking=()",
])


@dataclass
class HeadersConfig:
    hsts_max_age: int = 31536000   # 1 year
    hsts_preload: bool = True
    csp_default_src: list[str] = field(default_factory=lambda: ["'self'"])
    csp_connect_src: list[str] = field(default_factory=lambda: [
        "'self'", "wss:", "https:",
    ])
    csp_img_src: list[str] = field(default_factory=lambda: [
        "'self'", "data:", "blob:",
    ])
    csp_media_src: list[str] = field(default_factory=lambda: [
        "'self'", "data:", "blob:",
    ])
    csp_style_src: list[str] = field(default_factory=lambda: [
        "'self'", "'unsafe-inline'",   # admin uses inline styles
    ])
    csp_font_src: list[str] = field(default_factory=lambda: [
        "'self'", "data:",
    ])
    csp_frame_ancestors: list[str] = field(default_factory=lambda: ["'none'"])
    csp_report_uri: Optional[str] = "/api/security/csp-report"
    permissions_policy: str = DEFAULT_PERMISSIONS
    coep: str = "require-corp"
    coop: str = "same-origin"
    corp: str = "same-site"
    referrer_policy: str = "strict-origin-when-cross-origin"
    frame_options: str = "DENY"


class SecurityHeadersV2:
    def __init__(self, app: ASGIApp, config: Optional[HeadersConfig] = None) -> None:
        self.app = app
        self.cfg = config or HeadersConfig()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        nonce = base64.b64encode(os.urandom(16)).decode("ascii")
        state = scope.setdefault("state", {})
        if isinstance(state, dict):
            state["csp_nonce"] = nonce

        cfg = self.cfg

        async def _send(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                _put(headers, b"strict-transport-security", _hsts(cfg).encode("ascii"))
                _put(headers, b"x-content-type-options", b"nosniff")
                _put(headers, b"x-frame-options", cfg.frame_options.encode("ascii"))
                _put(headers, b"referrer-policy", cfg.referrer_policy.encode("ascii"))
                _put(headers, b"permissions-policy", cfg.permissions_policy.encode("ascii"))
                _put(headers, b"cross-origin-embedder-policy", cfg.coep.encode("ascii"))
                _put(headers, b"cross-origin-opener-policy", cfg.coop.encode("ascii"))
                _put(headers, b"cross-origin-resource-policy", cfg.corp.encode("ascii"))
                _put(headers, b"x-permitted-cross-domain-policies", b"none")
                _put(headers, b"server", b"helen")
                _put(headers, b"content-security-policy",
                     _build_csp(cfg, nonce).encode("ascii"))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, _send)


def _hsts(cfg: HeadersConfig) -> str:
    parts = [f"max-age={int(cfg.hsts_max_age)}", "includeSubDomains"]
    if cfg.hsts_preload:
        parts.append("preload")
    return "; ".join(parts)


def _build_csp(cfg: HeadersConfig, nonce: str) -> str:
    directives = [
        f"default-src {' '.join(cfg.csp_default_src)}",
        f"script-src 'self' 'nonce-{nonce}' 'strict-dynamic'",
        f"style-src {' '.join(cfg.csp_style_src)}",
        f"img-src {' '.join(cfg.csp_img_src)}",
        f"media-src {' '.join(cfg.csp_media_src)}",
        f"connect-src {' '.join(cfg.csp_connect_src)}",
        f"font-src {' '.join(cfg.csp_font_src)}",
        f"frame-ancestors {' '.join(cfg.csp_frame_ancestors)}",
        "base-uri 'self'",
        "form-action 'self'",
        "object-src 'none'",
        "worker-src 'self' blob:",
        "manifest-src 'self'",
    ]
    if cfg.csp_report_uri:
        directives.append(f"report-uri {cfg.csp_report_uri}")
    return "; ".join(directives)


def _put(headers: list[tuple[bytes, bytes]], name: bytes, value: bytes) -> None:
    lname = name.lower()
    for i, (k, _) in enumerate(headers):
        if k.lower() == lname:
            headers[i] = (name, value)
            return
    headers.append((name, value))


def attach_security_headers_v2(app, config: Optional[HeadersConfig] = None) -> SecurityHeadersV2:
    return SecurityHeadersV2(app, config)
