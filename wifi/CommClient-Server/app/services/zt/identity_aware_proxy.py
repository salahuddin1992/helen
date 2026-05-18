"""
Zero-Trust — Identity-Aware Proxy ASGI middleware.

Enforces full ZT evaluation on every request, not just JWT validation.

Order:
    1. Bypass list (health, well-known, OAuth callbacks).
    2. Extract identity from JWT-SVID header / cookie / bearer.
    3. Build a ``DecisionContext`` (device posture, IP, role, etc).
    4. Call the policy engine.
    5. Allow / deny / step-up.

Decisions are logged automatically. If the policy emits a
``require_mfa`` obligation, the response carries
``X-ZT-Step-Up: required``.
"""
from __future__ import annotations

import json
import time
from typing import Any, Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core.logging import get_logger
from app.services.zt.policy_engine import DecisionContext, get_policy_engine
from app.services.zt.spiffe_authority import verify_jwt

logger = get_logger(__name__)


BYPASS_PREFIXES = (
    "/api/edge/health",
    "/health",
    "/healthz",
    "/.well-known/",
    "/api/_federation/v2/handshake",
    "/api/zt/identity/me",  # caller may have no JWT yet; the route gates itself
)


class IdentityAwareProxy(BaseHTTPMiddleware):
    """ASGI middleware. Requires ``ZT_PROXY_ENABLED=1``."""

    def __init__(self, app, *, enabled: bool = True, fail_open: bool = False) -> None:
        super().__init__(app)
        self.enabled = enabled
        self.fail_open = fail_open

    async def dispatch(self, request: Request, call_next):
        if not self.enabled:
            return await call_next(request)
        path = request.url.path
        if any(path.startswith(p) for p in BYPASS_PREFIXES):
            return await call_next(request)

        identity = self._extract_identity(request)
        if identity is None:
            if self.fail_open:
                return await call_next(request)
            return JSONResponse(
                {"detail": "zt_identity_required"},
                status_code=401,
            )

        ctx = self._build_context(request, identity)
        # Pick the resource path & action from the request.
        resource = path
        action = request.method.lower()
        decision = await get_policy_engine().evaluate(
            ctx=ctx, resource=resource, action=action,
        )
        if not decision.allow:
            return JSONResponse(
                {
                    "detail":      "zt_denied",
                    "reasons":     decision.reasons,
                    "obligations": decision.obligations,
                },
                status_code=403,
            )
        # Annotate request state for downstream handlers.
        request.state.zt_identity = identity
        request.state.zt_decision = decision
        response: Response = await call_next(request)
        if "require_mfa" in decision.obligations:
            response.headers["X-ZT-Step-Up"] = "required"
        if "log_audit" in decision.obligations:
            response.headers["X-ZT-Audit"] = "logged"
        response.headers["X-ZT-Decision"] = "allow"
        return response

    # ── helpers ─────────────────────────────────────────────

    def _extract_identity(self, request: Request) -> Optional[dict[str, Any]]:
        token = ""
        auth = request.headers.get("authorization") or ""
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
        if not token:
            token = request.cookies.get("zt_svid") or ""
        if not token:
            token = request.headers.get("x-zt-svid") or ""
        if not token:
            return None
        return verify_jwt(token)

    def _build_context(
        self, request: Request, identity: dict[str, Any],
    ) -> DecisionContext:
        spiffe = identity.get("sub") or ""
        kind = identity.get("workload") or "user"
        role = identity.get("role") or ""
        workspace = identity.get("workspace")
        ip = (
            request.headers.get("x-real-ip")
            or (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
            or (request.client.host if request.client else "")
        )
        country = request.headers.get("x-country-code") or ""
        risk = int(request.headers.get("x-zt-risk") or 0)
        device_attested = request.headers.get("x-zt-device-attested") == "1"
        mfa_passed = request.headers.get("x-zt-mfa") == "1"
        session_id = request.cookies.get("zt_sid") or request.headers.get("x-zt-sid")
        return DecisionContext(
            identity=spiffe,
            workload_kind=kind,
            role=role,
            workspace=workspace,
            ip=ip,
            country=country,
            risk_score=risk,
            device_attested=device_attested,
            mfa_passed=mfa_passed,
            session_id=session_id,
        )
