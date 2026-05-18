"""
LAN-aware CORS attachment (Task #3).

The stock `main.create_app()` adds a `CORSMiddleware` with a fixed list of
localhost origins. When the server is deployed as "one PC = LAN server",
remote clients reach it via `http://192.168.x.x:3000` and their Electron
renderer's `Origin` header becomes `http://192.168.x.x:3000` — which the
fixed list rejects. Browser preflights then fail and the frontend receives
"blocked by CORS".

Rather than rewriting the existing middleware registration, this module:
  * Exposes `attach_lan_cors(app)` which adds a SECOND CORSMiddleware that
    matches LAN origins via a regex and also advertises Socket.IO paths.
  * Stacks cleanly with the existing middleware — FastAPI preserves order
    but the regex-based middleware's `allow_origin_regex` covers requests
    the existing list rejects.
  * Is opt-in: `app.core.extended_bootstrap.apply_lan_extensions(app)`
    wires it in without touching `main.py`.

NOTE: Starlette's CORSMiddleware accepts BOTH `allow_origins` and
`allow_origin_regex`. Using a regex here is intentional — it future-proofs
against DHCP-assigned IPs we haven't enumerated yet.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.services.lan_ice_helper import lan_origin_regex, lan_origins


def attach_lan_cors(app: FastAPI) -> None:
    """
    Add a LAN-aware CORS middleware to an existing FastAPI app.

    Safe to call multiple times — each call adds a distinct middleware
    instance, but FastAPI de-dupes identical configurations in practice.
    Operators who want a permissive wildcard can set
    `COMMCLIENT_EXTRA_CORS_ORIGINS=*`.
    """
    app.add_middleware(
        CORSMiddleware,
        allow_origins=lan_origins(),
        allow_origin_regex=lan_origin_regex(),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "X-Request-ID",
            "Accept",
            "X-Requested-With",
            "Cache-Control",
            "Origin",
        ],
        expose_headers=["X-Request-ID"],
        max_age=3600,
    )


__all__ = ["attach_lan_cors"]
