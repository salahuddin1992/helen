"""
app.domains.auth — Authentication & identity domain facade.

Aggregates: HTTP auth routes, JWT helpers, user model, session model,
password hashing, security utilities.

Existing implementation locations:
    app.api.routes.auth                — /api/auth/* router
    app.api.routes.sessions            — /api/sessions/* router
    app.services.auth_service          — authenticate_user(), etc.
    app.services.auth_token_pruner     — background token cleanup
    app.core.security                  — create_access_token, verify_token, hash_password
    app.core.security_utils            — password hashing helpers, constant-time compare
    app.models.user                    — User SQLAlchemy model
    app.models.session                 — Session model
"""

from __future__ import annotations

from app.domains._safe_import import safe_import

_exports: dict = {}

# Routers
_exports.update(safe_import("app.api.routes.auth", ["router"]))
if "router" in _exports:
    _exports["auth_router"] = _exports.pop("router")
_exports.update(safe_import("app.api.routes.sessions", ["router"]))
if "router" in _exports:
    _exports["sessions_router"] = _exports.pop("router")

# Services
_exports.update(safe_import(
    "app.services.auth_service",
    [
        "authenticate_user",
        "create_user",
        "issue_tokens",
        "refresh_tokens",
        "revoke_session",
        "verify_credentials",
    ],
))
_exports.update(safe_import(
    "app.services.auth_token_pruner",
    ["prune_expired_tokens", "start_token_pruner", "stop_token_pruner"],
))

# Core JWT / security primitives
_exports.update(safe_import(
    "app.core.security",
    [
        "create_access_token",
        "create_refresh_token",
        "verify_token",
        "decode_token",
        "hash_password",
        "verify_password",
        "ALGORITHM",
        "SECRET_KEY",
    ],
))
_exports.update(safe_import(
    "app.core.security_utils",
    [
        "constant_time_compare",
        "generate_csrf_token",
        "sanitize_username",
    ],
))

# Models
_exports.update(safe_import("app.models.user", ["User", "UserRole"]))
_exports.update(safe_import("app.models.session", ["Session"]))

globals().update(_exports)
__all__ = sorted(_exports.keys())
