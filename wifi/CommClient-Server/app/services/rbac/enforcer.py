"""
Phase 2 / Module G — RBAC enforcement service.

* ``user_has_permission(db, user_id, key)`` — async predicate.
* ``get_user_permissions(db, user_id)`` — set of effective permission keys.
* ``require_permission(key)`` — FastAPI dependency factory that returns
  the JWT subject ID on success and raises 403 otherwise.

The result is cached per ``user_id`` for 5 minutes with a tiny LRU.
Invalidate manually via ``invalidate(user_id)`` after a role change.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_permission_denied
from app.core.deps import get_db
from app.core.logging import get_logger
from app.core.security import decode_token
from app.models.rbac import Permission, Role, RolePermission, UserRole
from app.models.user import User
from app.services.rbac.registry import SUPERADMIN_ROLE_NAME

logger = get_logger(__name__)

_security = HTTPBearer()

_CACHE_TTL_SEC = 300
_CACHE_MAX = 1024


class _PermCache:
    """Tiny TTL+LRU keyed on user_id."""

    def __init__(self, ttl: float = _CACHE_TTL_SEC, max_size: int = _CACHE_MAX) -> None:
        self._ttl = ttl
        self._max = max_size
        self._lock = asyncio.Lock()
        self._d: "OrderedDict[str, tuple[float, set[str]]]" = OrderedDict()

    async def get(self, user_id: str) -> Optional[set[str]]:
        async with self._lock:
            entry = self._d.get(user_id)
            if not entry:
                return None
            ts, perms = entry
            if time.time() - ts > self._ttl:
                self._d.pop(user_id, None)
                return None
            self._d.move_to_end(user_id)
            return set(perms)

    async def put(self, user_id: str, perms: set[str]) -> None:
        async with self._lock:
            self._d[user_id] = (time.time(), set(perms))
            self._d.move_to_end(user_id)
            while len(self._d) > self._max:
                self._d.popitem(last=False)

    async def invalidate(self, user_id: str) -> None:
        async with self._lock:
            self._d.pop(user_id, None)

    async def clear(self) -> None:
        async with self._lock:
            self._d.clear()


_cache = _PermCache()


# ── Legacy role bridging ───────────────────────────────────

_LEGACY_TO_NEW: dict[str, str] = {
    "admin": "admin",
    "moderator": "moderator",
    "user": "member",
}


# ── Core resolution ────────────────────────────────────────

async def _resolve_permissions(db: AsyncSession, user_id: str) -> set[str]:
    """Compute the union of every permission attached to every role the
    user holds, plus their legacy role mapping."""
    perms: set[str] = set()

    # 1) New-system roles via UserRole
    role_rows = (await db.execute(
        select(Role).join(UserRole, UserRole.role_id == Role.id)
        .where(UserRole.user_id == user_id)
    )).scalars().all()
    role_names = {r.name for r in role_rows}

    # 2) Legacy role from users.role
    legacy = (await db.execute(
        select(User.role).where(User.id == user_id)
    )).scalar_one_or_none()
    if legacy and legacy in _LEGACY_TO_NEW:
        role_names.add(_LEGACY_TO_NEW[legacy])

    if not role_names:
        return perms

    # Superadmin = everything in the catalogue
    if SUPERADMIN_ROLE_NAME in role_names:
        every = (await db.execute(select(Permission.key))).scalars().all()
        return set(every)

    # 3) Pull every permission attached to any matching role
    rows = (await db.execute(
        select(Permission.key, RolePermission.granted)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .join(Role, Role.id == RolePermission.role_id)
        .where(Role.name.in_(role_names))
    )).all()
    for key, granted in rows:
        if granted:
            perms.add(key)
    return perms


async def get_user_permissions(db: AsyncSession, user_id: str) -> set[str]:
    cached = await _cache.get(user_id)
    if cached is not None:
        return cached
    perms = await _resolve_permissions(db, user_id)
    await _cache.put(user_id, perms)
    return perms


async def user_has_permission(
    db: AsyncSession, user_id: str, key: str,
) -> bool:
    perms = await get_user_permissions(db, user_id)
    return key in perms


async def invalidate(user_id: str) -> None:
    """Drop the cached permission set for a user — call this after any
    role / role-permission edit affecting them."""
    await _cache.invalidate(user_id)


async def invalidate_all() -> None:
    await _cache.clear()


# ── FastAPI dependency ─────────────────────────────────────

def require_permission(key: str):
    """Return a FastAPI dependency that yields ``user_id`` on success and
    raises 403 otherwise. The token is verified the same way as
    ``require_role`` — access token + sub claim mandatory."""
    async def _dep(
        credentials: HTTPAuthorizationCredentials = Depends(_security),
        db: AsyncSession = Depends(get_db),
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
        if not await user_has_permission(db, user_id, key):
            audit_permission_denied(
                user_id=user_id, resource=f"permission:{key}",
                action="access",
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Missing permission: {key}",
            )
        return user_id

    return _dep
