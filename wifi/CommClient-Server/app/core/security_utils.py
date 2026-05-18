"""
Security utility functions: UUID validation, RBAC enforcement, input sanitization.

Provides FastAPI dependencies for role-based access control and
reusable validators for socket event handlers.
"""

from __future__ import annotations

import re
from functools import wraps
from typing import Any, Callable
from uuid import UUID

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.audit import audit_log, audit_permission_denied
from app.core.logging import get_logger
from app.core.security import decode_token

logger = get_logger(__name__)

security_scheme = HTTPBearer()

# ── Role Hierarchy ─────────────────────────────────────────
# Higher level = more privileges
ROLE_LEVELS = {
    "user": 0,
    "moderator": 1,
    "admin": 2,
}

VALID_ROLES = set(ROLE_LEVELS.keys())


def role_level(role: str) -> int:
    """Get numeric level for a role. Unknown roles get level -1."""
    return ROLE_LEVELS.get(role, -1)


def has_role(user_role: str, required_role: str) -> bool:
    """
    Check if user_role meets or exceeds the required_role level.
    Returns False if either role is unknown/invalid.
    """
    if user_role not in VALID_ROLES or required_role not in VALID_ROLES:
        return False
    return role_level(user_role) >= role_level(required_role)


# ── FastAPI Dependencies for RBAC ──────────────────────────

def require_role(minimum_role: str):
    """
    FastAPI dependency factory: returns a dependency that extracts user_id
    and verifies the JWT contains at least the specified role.

    Usage:
        @router.post("/admin/kick/{target_id}")
        async def kick(target_id: str, user_id: str = Depends(require_role("admin"))):
            ...
    """
    async def _dependency(
        credentials: HTTPAuthorizationCredentials = Depends(security_scheme),
    ) -> str:
        payload = decode_token(credentials.credentials)

        if payload.get("type") != "access":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token type — expected access token",
            )

        user_id = payload.get("sub")
        if not user_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token missing subject claim",
            )

        user_role = payload.get("role", "user")
        if not has_role(user_role, minimum_role):
            audit_permission_denied(
                user_id=user_id,
                resource=f"role:{minimum_role}",
                action="access",
            )
            logger.warning(
                "rbac_access_denied",
                user_id=user_id,
                user_role=user_role,
                required_role=minimum_role,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient permissions",
            )

        return user_id

    return _dependency


def get_current_user_with_role(
    credentials: HTTPAuthorizationCredentials = Depends(security_scheme),
) -> tuple[str, str]:
    """
    FastAPI dependency: extract user_id AND role from JWT.
    Returns (user_id, role) tuple.

    Usage:
        async def endpoint(user_info: tuple = Depends(get_current_user_with_role)):
            user_id, role = user_info
    """
    payload = decode_token(credentials.credentials)
    if payload.get("type") != "access":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token type",
        )
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token missing subject claim",
        )
    role = payload.get("role", "user")
    return user_id, role


# ── Socket.IO Role Check Helper ───────────────────────────

def socket_has_role(token_payload: dict[str, Any], required_role: str) -> bool:
    """
    Check if a decoded socket JWT payload has sufficient role.
    Used in socket event handlers after get_user_id().
    """
    user_role = token_payload.get("role", "user")
    return has_role(user_role, required_role)


# ── UUID Validation ────────────────────────────────────────

# Pre-compiled regex for UUID v4 format
_UUID_REGEX = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
    re.IGNORECASE,
)


def validate_uuid(value: str | None, field_name: str = "id") -> str:
    """
    Validate that a string is a valid UUID format.
    Returns the normalized lowercase UUID string.
    Raises ValueError with descriptive message on failure.
    """
    if not value or not isinstance(value, str):
        raise ValueError(f"Invalid {field_name}: must be a non-empty string")

    value = value.strip().lower()

    if not _UUID_REGEX.match(value):
        raise ValueError(f"Invalid {field_name}: not a valid UUID format")

    # Final validation via stdlib
    try:
        UUID(value)
    except (ValueError, AttributeError):
        raise ValueError(f"Invalid {field_name}: malformed UUID")

    return value


def is_valid_uuid(value: str | None) -> bool:
    """Quick check if a value is a valid UUID. Returns bool, never raises."""
    if not value or not isinstance(value, str):
        return False
    try:
        UUID(value.strip())
        return True
    except (ValueError, AttributeError):
        return False


# ── Input Sanitization ─────────────────────────────────────

def sanitize_string(
    value: str | None,
    max_length: int = 1000,
    field_name: str = "input",
    allow_empty: bool = False,
) -> str:
    """
    Sanitize and validate a string input.
    Strips whitespace, enforces length, rejects null bytes.
    """
    if value is None:
        if allow_empty:
            return ""
        raise ValueError(f"{field_name} is required")

    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")

    value = value.strip()

    if not allow_empty and not value:
        raise ValueError(f"{field_name} cannot be empty")

    if len(value) > max_length:
        raise ValueError(f"{field_name} exceeds maximum length of {max_length}")

    # Reject null bytes (potential injection vector)
    if '\x00' in value:
        raise ValueError(f"{field_name} contains invalid characters")

    return value


# ── Channel Membership Cache ──────────────────────────────
# Lightweight in-memory cache to reduce DB queries for membership checks
# in high-frequency socket handlers (typing, read receipts, etc.)

import time
from collections import OrderedDict

_membership_cache: OrderedDict[tuple[str, str], tuple[bool, float]] = OrderedDict()
_membership_cache_max = 5_000
_membership_cache_ttl = 30.0  # seconds


def cache_membership(channel_id: str, user_id: str, is_member: bool) -> None:
    """Cache a channel membership check result."""
    key = (channel_id, user_id)
    while len(_membership_cache) >= _membership_cache_max:
        _membership_cache.popitem(last=False)
    _membership_cache[key] = (is_member, time.monotonic())


def get_cached_membership(channel_id: str, user_id: str) -> bool | None:
    """
    Get cached membership result.
    Returns True/False if cached and fresh, None if not cached or stale.
    """
    key = (channel_id, user_id)
    if key not in _membership_cache:
        return None
    is_member, ts = _membership_cache[key]
    if time.monotonic() - ts > _membership_cache_ttl:
        del _membership_cache[key]
        return None
    return is_member


def invalidate_membership_cache(channel_id: str, user_id: str | None = None) -> None:
    """
    Invalidate membership cache for a channel.
    If user_id is None, invalidates all users for that channel.
    """
    if user_id:
        _membership_cache.pop((channel_id, user_id), None)
    else:
        keys_to_remove = [k for k in _membership_cache if k[0] == channel_id]
        for k in keys_to_remove:
            del _membership_cache[k]
