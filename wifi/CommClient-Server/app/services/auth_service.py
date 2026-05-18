"""
Authentication service — registration, login, token management.
"""

from __future__ import annotations

from app.core.crypto import hash_refresh_token
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_log
from app.core.config import get_settings
from app.core.exceptions import ConflictError, NotFoundError
from app.core.logging import get_logger
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    hash_password_async,
    verify_password,
    verify_password_async,
)
from app.core.share_code import generate_share_code
from app.models.session import UserSession
from app.models.user import User


async def _mint_unique_share_code(db: AsyncSession, max_attempts: int = 8) -> str:
    """Generate a share_code not already present in the users table.

    Collisions are astronomically unlikely (62^64 space), but we still
    verify to be robust against any future entropy degradation.
    """
    for _ in range(max_attempts):
        code = generate_share_code()
        existing = await db.execute(select(User.id).where(User.share_code == code))
        if existing.first() is None:
            return code
    # If we somehow fail 8 times, let the DB unique constraint catch it.
    return generate_share_code()

logger = get_logger(__name__)
settings = get_settings()


class AuthService:

    @staticmethod
    async def register(
        db: AsyncSession,
        username: str,
        display_name: str,
        password: str,
        avatar_url: str | None = None,
        bio: str | None = None,
    ) -> tuple[User, str, str]:
        """Register a new user. Returns (user, access_token, refresh_token)."""

        # Check uniqueness
        existing = await db.execute(select(User).where(User.username == username))
        if existing.scalar_one_or_none():
            raise ConflictError(f"Username '{username}' is already taken")

        # First-user bootstrap: if no users exist yet, promote to admin
        user_count_result = await db.execute(select(User))
        is_first_user = len(user_count_result.scalars().all()) == 0

        # Hash bcrypt-style under a bounded semaphore so a stampede of
        # concurrent registrations doesn't pin every executor thread on
        # bcrypt and starve the asyncio event loop (megascale fix).
        password_hash_value = await hash_password_async(password)
        user = User(
            username=username,
            share_code=await _mint_unique_share_code(db),
            display_name=display_name,
            password_hash=password_hash_value,
            avatar_url=avatar_url,
            bio=bio,
            status="online",
            role="admin" if is_first_user else "user",
        )
        db.add(user)
        await db.flush()

        access_token = create_access_token(user.id, role=user.role)
        refresh_token = create_refresh_token(user.id)

        # Record session
        session = UserSession(
            user_id=user.id,
            token_hash=hash_refresh_token(refresh_token),
            expires_at=datetime.now(timezone.utc) + timedelta(days=settings.JWT_REFRESH_TOKEN_EXPIRE_DAYS),
        )
        db.add(session)
        await db.commit()
        await db.refresh(user)

        logger.info("user_registered", user_id=user.id, username=username)
        return user, access_token, refresh_token

    @staticmethod
    async def login(
        db: AsyncSession,
        username: str,
        password: str,
        device_name: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> tuple[User, str, str]:
        """Authenticate a user. Returns (user, access_token, refresh_token)."""

        result = await db.execute(select(User).where(User.username == username))
        user = result.scalar_one_or_none()

        # Same bounded-semaphore reasoning as register: bcrypt verify is
        # ~250 ms of CPU per call. Run it in the executor under the auth
        # semaphore so login storms don't wedge the event loop.
        # Always do the verify (against a dummy hash if user is missing)
        # so attackers can't tell whether a username exists from response
        # timing — the cost is one extra bcrypt verify on misses.
        _DUMMY_HASH = "$2b$12$" + "C" * 53
        provided_hash = user.password_hash if user else _DUMMY_HASH
        password_ok = await verify_password_async(password, provided_hash)
        if not user or not password_ok:
            raise NotFoundError("User", username)

        if not user.is_active:
            raise ConflictError("Account is deactivated")

        # Update presence
        user.status = "online"
        user.last_seen = datetime.now(timezone.utc)

        access_token = create_access_token(user.id, role=user.role)
        refresh_token = create_refresh_token(user.id)

        # Enforce per-user session cap: evict the oldest active sessions until
        # room exists for the new one. Cap <= 0 disables the check.
        await AuthService._enforce_session_cap(
            db, user_id=user.id, ip_address=ip_address
        )

        # Record session
        session = UserSession(
            user_id=user.id,
            token_hash=hash_refresh_token(refresh_token),
            device_name=device_name,
            ip_address=ip_address,
            user_agent=user_agent,
            expires_at=datetime.now(timezone.utc) + timedelta(days=settings.JWT_REFRESH_TOKEN_EXPIRE_DAYS),
        )
        db.add(session)
        await db.commit()
        await db.refresh(user)

        logger.info("user_login", user_id=user.id, username=username, role=user.role)
        return user, access_token, refresh_token

    @staticmethod
    async def _enforce_session_cap(
        db: AsyncSession,
        user_id: str,
        ip_address: str | None = None,
    ) -> None:
        """Revoke oldest active sessions so count stays < MAX_SESSIONS_PER_USER.

        Called just before creating a new session on login so that the new
        session plus surviving sessions total at most the cap.
        """
        cap = settings.MAX_SESSIONS_PER_USER
        if cap is None or cap <= 0:
            return

        now = datetime.now(timezone.utc)
        result = await db.execute(
            select(UserSession)
            .where(
                UserSession.user_id == user_id,
                UserSession.is_active == True,  # noqa: E712
                UserSession.expires_at > now,
            )
            .order_by(UserSession.last_activity.asc())
        )
        active = list(result.scalars())
        # We're about to add one more session — allow (cap - 1) to survive.
        to_evict_count = len(active) - (cap - 1)
        if to_evict_count <= 0:
            return

        for victim in active[:to_evict_count]:
            victim.is_active = False
            audit_log(
                "auth.session_auto_revoked",
                user_id=user_id,
                ip_address=ip_address,
                details={
                    "evicted_session_id": victim.id,
                    "evicted_device_name": victim.device_name,
                    "evicted_ip_address": victim.ip_address,
                    "cap": cap,
                    "reason": "session_cap_exceeded",
                },
            )
        logger.info(
            "session_cap_enforced",
            user_id=user_id,
            evicted=to_evict_count,
            cap=cap,
        )

    @staticmethod
    async def refresh_tokens(
        db: AsyncSession,
        refresh_token_str: str,
    ) -> tuple[str, str]:
        """Refresh access and refresh tokens. Rotates the refresh token."""

        payload = decode_token(refresh_token_str)
        if payload.get("type") != "refresh":
            raise ConflictError("Invalid token type — expected refresh token")

        user_id = payload["sub"]
        old_hash = hash_refresh_token(refresh_token_str)

        # Find and invalidate old session
        result = await db.execute(
            select(UserSession).where(
                UserSession.user_id == user_id,
                UserSession.token_hash == old_hash,
                UserSession.is_active == True,
            )
        )
        old_session = result.scalar_one_or_none()
        if not old_session:
            raise ConflictError("Refresh token not found or already revoked")

        old_session.is_active = False

        # Look up current role from DB to embed in new token
        user_result = await db.execute(select(User).where(User.id == user_id))
        user = user_result.scalar_one_or_none()
        current_role = user.role if user else "user"

        # Issue new pair
        new_access = create_access_token(user_id, role=current_role)
        new_refresh = create_refresh_token(user_id)

        new_session = UserSession(
            user_id=user_id,
            token_hash=hash_refresh_token(new_refresh),
            device_name=old_session.device_name,
            ip_address=old_session.ip_address,
            user_agent=old_session.user_agent,
            expires_at=datetime.now(timezone.utc) + timedelta(days=settings.JWT_REFRESH_TOKEN_EXPIRE_DAYS),
        )
        db.add(new_session)
        await db.commit()

        logger.info("token_refreshed", user_id=user_id)
        return new_access, new_refresh

    @staticmethod
    async def logout(
        db: AsyncSession,
        user_id: str,
        refresh_token_str: str | None = None,
    ) -> None:
        """Logout — revoke specific session or all sessions."""
        if refresh_token_str:
            token_hash = hash_refresh_token(refresh_token_str)
            result = await db.execute(
                select(UserSession).where(
                    UserSession.user_id == user_id,
                    UserSession.token_hash == token_hash,
                )
            )
            session = result.scalar_one_or_none()
            if session:
                session.is_active = False
        else:
            # Revoke all sessions
            result = await db.execute(
                select(UserSession).where(
                    UserSession.user_id == user_id,
                    UserSession.is_active == True,
                )
            )
            for session in result.scalars():
                session.is_active = False

        # Set user offline
        result = await db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        if user:
            user.status = "offline"
            user.last_seen = datetime.now(timezone.utc)

        await db.commit()
        logger.info("user_logout", user_id=user_id)
