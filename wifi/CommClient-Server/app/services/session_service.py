"""
Device / session management service.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ForbiddenError, NotFoundError
from app.core.logging import get_logger
from app.models.session import UserSession

logger = get_logger(__name__)


class SessionService:

    @staticmethod
    async def list_sessions(
        db: AsyncSession,
        user_id: str,
    ) -> list[UserSession]:
        """List all active sessions for a user."""
        result = await db.execute(
            select(UserSession)
            .where(
                UserSession.user_id == user_id,
                UserSession.is_active == True,
            )
            .order_by(UserSession.last_activity.desc())
        )
        return list(result.scalars().all())

    @staticmethod
    async def revoke_session(
        db: AsyncSession,
        user_id: str,
        session_id: str,
    ) -> None:
        """Revoke a specific session (force logout that device)."""
        result = await db.execute(
            select(UserSession).where(UserSession.id == session_id)
        )
        session = result.scalar_one_or_none()
        if not session:
            raise NotFoundError("Session", session_id)
        if session.user_id != user_id:
            raise ForbiddenError("Cannot revoke another user's session")

        session.is_active = False
        await db.commit()
        logger.info("session_revoked", session_id=session_id, user_id=user_id)

    @staticmethod
    async def revoke_all_sessions(
        db: AsyncSession,
        user_id: str,
        except_session_id: str | None = None,
    ) -> int:
        """Revoke all sessions for a user (except optionally the current one)."""
        result = await db.execute(
            select(UserSession).where(
                UserSession.user_id == user_id,
                UserSession.is_active == True,
            )
        )
        count = 0
        for session in result.scalars():
            if except_session_id and session.id == except_session_id:
                continue
            session.is_active = False
            count += 1
        await db.commit()
        logger.info("sessions_revoked_all", user_id=user_id, count=count)
        return count

    @staticmethod
    async def admin_revoke_session(
        db: AsyncSession,
        target_user_id: str,
        session_id: str,
    ) -> None:
        """Admin variant of revoke_session — skips the self-ownership check.

        The route layer must enforce that the caller holds the admin role
        before reaching this method. Still validates that the session
        belongs to the claimed user_id to avoid a typo revoking the wrong
        row.
        """
        result = await db.execute(
            select(UserSession).where(UserSession.id == session_id)
        )
        session = result.scalar_one_or_none()
        if not session:
            raise NotFoundError("Session", session_id)
        if session.user_id != target_user_id:
            raise NotFoundError("Session", f"{session_id}/{target_user_id}")
        session.is_active = False
        await db.commit()
        logger.info(
            "admin_session_revoked",
            session_id=session_id,
            target_user_id=target_user_id,
        )

    @staticmethod
    async def admin_revoke_all_for_user(
        db: AsyncSession,
        target_user_id: str,
    ) -> int:
        """Force-logout every active session for the given user."""
        result = await db.execute(
            select(UserSession).where(
                UserSession.user_id == target_user_id,
                UserSession.is_active == True,  # noqa: E712
            )
        )
        count = 0
        for session in result.scalars():
            session.is_active = False
            count += 1
        await db.commit()
        logger.info("admin_sessions_revoked_all", target_user_id=target_user_id, count=count)
        return count

    @staticmethod
    async def update_activity(
        db: AsyncSession,
        session_id: str,
    ) -> None:
        """Update last activity timestamp for a session."""
        result = await db.execute(
            select(UserSession).where(UserSession.id == session_id)
        )
        session = result.scalar_one_or_none()
        if session:
            session.last_activity = datetime.now(timezone.utc)
            await db.commit()
