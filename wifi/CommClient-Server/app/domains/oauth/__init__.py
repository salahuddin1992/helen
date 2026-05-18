"""
app.domains.oauth — External OAuth providers (Phase 3 Module N).

Existing implementation locations:
    app.api.routes.oauth        — /api/oauth/* router (authorize/callback)
    app.services.oauth_service  — provider abstractions, flow
    app.models.oauth            — OAuthProvider, OAuthAccount ORM
"""

from __future__ import annotations

from app.domains._safe_import import safe_import

_exports: dict = {}

got = safe_import("app.api.routes.oauth", ["router"])
if "router" in got:
    _exports["oauth_router"] = got["router"]

_exports.update(safe_import(
    "app.services.oauth_service",
    [
        "OAuthService",
        "build_authorize_url",
        "exchange_code",
        "fetch_userinfo",
        "ProviderAdapter",
    ],
))

_exports.update(safe_import(
    "app.models.oauth",
    ["OAuthProvider", "OAuthAccount", "OAuthState"],
))

globals().update(_exports)
__all__ = sorted(_exports.keys())
