"""
Embedded read-only reports.

Signs a short-lived JWT bound to:

  * the workspace
  * the dashboard ID
  * an optional viewer email (for audit)
  * an expiry (default 24 h)

The verifier returns the dashboard ID + scope on success; the route
handler is then responsible for serving the read-only widgets.

Signing key is reused from Helen's main JWT config to avoid a second
secret to manage.
"""
from __future__ import annotations

import os
import time
from typing import Any, Optional

from app.core.logging import get_logger

logger = get_logger(__name__)


try:                                                                  # pragma: no cover
    import jwt as _jwt                                                # type: ignore[import-untyped]
    _JWT_AVAILABLE = True
except Exception:                                                     # noqa: BLE001
    _jwt = None                                                        # type: ignore[assignment]
    _JWT_AVAILABLE = False


SECRET = os.getenv("HELEN_JWT_SECRET", "change-me-in-prod")
ALG = os.getenv("HELEN_JWT_ALG", "HS256")
DEFAULT_TTL = 86_400


def sign_embed_token(
    *, workspace_id: str, dashboard_id: str,
    viewer_email: Optional[str] = None,
    ttl_seconds: int = DEFAULT_TTL,
    extra: Optional[dict[str, Any]] = None,
) -> str:
    if not _JWT_AVAILABLE:
        raise RuntimeError("pyjwt not installed")
    now = int(time.time())
    payload: dict[str, Any] = {
        "iss": "helen-analytics",
        "iat": now,
        "exp": now + max(60, ttl_seconds),
        "scope": "embed:read",
        "workspace_id": workspace_id,
        "dashboard_id": dashboard_id,
    }
    if viewer_email:
        payload["viewer"] = viewer_email
    if extra:
        payload.update(extra)
    return _jwt.encode(payload, SECRET, algorithm=ALG)                  # type: ignore[union-attr]


def verify_embed_token(token: str) -> Optional[dict[str, Any]]:
    if not _JWT_AVAILABLE:
        return None
    try:
        payload = _jwt.decode(token, SECRET, algorithms=[ALG])           # type: ignore[union-attr]
    except Exception as e:                                              # noqa: BLE001
        logger.warning("embed.token.invalid: %s", e)
        return None
    if payload.get("scope") != "embed:read":
        return None
    return payload


def embed_url(
    *, base_url: str, dashboard_id: str, token: str,
) -> str:
    return f"{base_url.rstrip('/')}/api/analytics/embed/{dashboard_id}?token={token}"
