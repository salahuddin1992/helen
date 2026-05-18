"""
Security utilities: password hashing, JWT token creation/verification.

Hardened:
  - JTI (JWT ID) in every token for revocation support
  - Token fingerprint binding (optional IP+UA check)
  - Error messages sanitized (no internal details leaked)
  - Bcrypt cost factor 12
  - Token type enforcement
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
import jwt
from fastapi import HTTPException, status

from app.core.config import get_settings

settings = get_settings()

# ── In-memory revoked JTI store (for access token blacklisting) ──
# Uses OrderedDict as LRU cache — evicts oldest entries instead of clearing all.
# For LAN-only deployments, in-memory is acceptable (no Redis needed).
from collections import OrderedDict

_revoked_jtis: OrderedDict[str, float] = OrderedDict()  # jti → revocation_timestamp
_revoked_jtis_max = 10_000


def revoke_jti(jti: str) -> None:
    """Add a JTI to the revocation store with FIFO eviction."""
    import time
    while len(_revoked_jtis) >= _revoked_jtis_max:
        _revoked_jtis.popitem(last=False)  # evict oldest (FIFO)
    _revoked_jtis[jti] = time.monotonic()


def is_jti_revoked(jti: str) -> bool:
    """Check if a JTI has been revoked."""
    return jti in _revoked_jtis


def cleanup_expired_jtis(max_age_seconds: int = 3600) -> int:
    """Remove JTIs older than max_age_seconds. Returns count removed."""
    import time
    cutoff = time.monotonic() - max_age_seconds
    expired = [jti for jti, ts in _revoked_jtis.items() if ts < cutoff]
    for jti in expired:
        del _revoked_jtis[jti]
    return len(expired)


# ── Password Hashing ────────────────────────────────────────
#
# bcrypt at cost 12 takes ≈250 ms of CPU per call. The default executor
# pool is min(32, cpu+4), so without a bounded queue 100 concurrent
# /api/auth/register calls would each acquire a thread and contend for the
# CPU, blocking the asyncio event loop and starving every other request
# (this is exactly what the e2e-megascale benchmark observed: server
# stopped responding to /api/health for 30 seconds and crashed at N=100).
#
# Fix: cap concurrent bcrypt ops at ~CPU/2 via an asyncio.Semaphore. Extra
# callers wait FIFO inside the semaphore instead of hammering the executor
# pool. The wait shows up as honest auth latency rather than a wedged
# event loop.
import asyncio
import os

# LAN-deployment tuning. Trusted-LAN servers value throughput over
# offline-attack hardness, so allow operators to:
#   * Use the full CPU pool (HELEN_BCRYPT_PARALLEL=cpu) instead of cpu/2.
#   * Lower bcrypt cost from 12 → 10 (still strong; 4× faster auth).
# Defaults stay safe for an internet-exposed server.
_BCRYPT_MAX_PARALLEL = max(
    2,
    int(os.environ.get("HELEN_BCRYPT_PARALLEL", "0"))
    or ((os.cpu_count() or 4) // 2),
)
_BCRYPT_COST = max(8, min(15, int(os.environ.get("HELEN_BCRYPT_COST", "12"))))
_bcrypt_sem: asyncio.Semaphore | None = None


def _get_bcrypt_sem() -> asyncio.Semaphore:
    global _bcrypt_sem
    if _bcrypt_sem is None:
        _bcrypt_sem = asyncio.Semaphore(_BCRYPT_MAX_PARALLEL)
    return _bcrypt_sem


def hash_password(password: str) -> str:
    """Hash a password with bcrypt (default cost 12, override via
    ``HELEN_BCRYPT_COST`` env, range 8-15). Sync entry point — callers in
    async contexts should prefer ``hash_password_async``."""
    return bcrypt.hashpw(
        password.encode("utf-8"), bcrypt.gensalt(rounds=_BCRYPT_COST)
    ).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """Verify a password against its bcrypt hash (constant-time)."""
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


async def hash_password_async(password: str) -> str:
    """Async-safe bcrypt hashing. Runs in the default thread executor under
    a semaphore so concurrent register/reset-password storms don't pin every
    worker thread on bcrypt and starve the event loop."""
    async with _get_bcrypt_sem():
        return await asyncio.to_thread(hash_password, password)


async def verify_password_async(plain: str, hashed: str) -> bool:
    """Async-safe bcrypt verify. Same semaphore-bounded executor pool as
    ``hash_password_async`` so login storms behave gracefully."""
    async with _get_bcrypt_sem():
        return await asyncio.to_thread(verify_password, plain, hashed)


# ── JWT Tokens ──────────────────────────────────────────────

def create_access_token(
    user_id: str,
    role: str = "user",
    extra: dict[str, Any] | None = None,
    fingerprint: str | None = None,
) -> str:
    """
    Create a short-lived JWT access token.
    Includes JTI for revocation, role claim for RBAC, and optional fingerprint for binding.
    """
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": user_id,
        "type": "access",
        "role": role,
        "jti": secrets.token_hex(16),
        "iat": now,
        "exp": now + timedelta(minutes=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES),
    }
    if fingerprint:
        payload["fpr"] = fingerprint
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def create_refresh_token(user_id: str) -> str:
    """Create a long-lived JWT refresh token with JTI."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "type": "refresh",
        "jti": secrets.token_hex(16),
        "iat": now,
        "exp": now + timedelta(days=settings.JWT_REFRESH_TOKEN_EXPIRE_DAYS),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def decode_token(token: str) -> dict[str, Any]:
    """
    Decode and validate a JWT token. Raises HTTPException on failure.
    Checks: signature, expiry, required claims, JTI revocation.
    """
    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET,
            algorithms=[settings.JWT_ALGORITHM],
            options={"require": ["sub", "type", "exp", "iat"]},
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
        )
    except jwt.InvalidTokenError:
        # SECURITY: Do NOT leak the specific decode error to the client
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token",
        )

    # Check JTI revocation
    jti = payload.get("jti")
    if jti and is_jti_revoked(jti):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has been revoked",
        )

    return payload


def decode_token_no_http(token: str) -> dict[str, Any] | None:
    """Decode a JWT token without raising HTTP exceptions (for Socket.IO auth)."""
    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET,
            algorithms=[settings.JWT_ALGORITHM],
            options={"require": ["sub", "type", "exp", "iat"]},
        )
        # Check JTI revocation
        jti = payload.get("jti")
        if jti and is_jti_revoked(jti):
            return None
        return payload
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return None
