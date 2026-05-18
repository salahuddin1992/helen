"""
User management service — profiles, search, contacts.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError
from app.core.logging import get_logger
from app.models.contact import Contact
from app.models.user import User

logger = get_logger(__name__)


class UserService:

    @staticmethod
    async def get_user(db: AsyncSession, user_id: str) -> User:
        result = await db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        if not user:
            raise NotFoundError("User", user_id)
        return user

    @staticmethod
    async def get_user_by_username(db: AsyncSession, username: str) -> User:
        result = await db.execute(select(User).where(User.username == username))
        user = result.scalar_one_or_none()
        if not user:
            raise NotFoundError("User", username)
        return user

    @staticmethod
    async def list_users(
        db: AsyncSession,
        skip: int = 0,
        limit: int = 50,
        search: str | None = None,
    ) -> tuple[list[User], int]:
        query = select(User).where(User.is_active == True)
        count_query = select(func.count()).select_from(User).where(User.is_active == True)

        if search:
            pattern = f"%{search}%"
            search_filter = or_(
                User.username.ilike(pattern),
                User.display_name.ilike(pattern),
            )
            query = query.where(search_filter)
            count_query = count_query.where(search_filter)

        total = (await db.execute(count_query)).scalar() or 0
        result = await db.execute(
            query.order_by(User.display_name).offset(skip).limit(limit)
        )
        return list(result.scalars().all()), total

    # Fields where setting None should EXPLICITLY clear the value rather than skip
    _NULLABLE_CLEAR_FIELDS = frozenset({
        "status_message",
        "status_expires_at",
        "bio",
        "avatar_url",
    })

    @staticmethod
    async def update_user(
        db: AsyncSession,
        user_id: str,
        **kwargs,
    ) -> User:
        user = await UserService.get_user(db, user_id)
        for key, value in kwargs.items():
            if not hasattr(user, key):
                continue
            # Allow explicit clear (None) for nullable fields; skip None for required fields
            if value is None and key not in UserService._NULLABLE_CLEAR_FIELDS:
                continue
            setattr(user, key, value)
        user.updated_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(user)
        logger.info("user_updated", user_id=user_id, fields=list(kwargs.keys()))
        return user

    @staticmethod
    async def set_status_message(
        db: AsyncSession,
        user_id: str,
        status_message: str | None,
        status_expires_at: datetime | None = None,
    ) -> User:
        """
        Set or clear a custom user status message.
        Pass status_message=None to clear it.
        """
        user = await UserService.get_user(db, user_id)
        if status_message is not None and len(status_message) > 140:
            raise ValueError("status_message exceeds 140 characters")
        user.status_message = status_message
        user.status_expires_at = status_expires_at if status_message else None
        user.updated_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(user)
        logger.info(
            "user_status_message_set",
            user_id=user_id,
            cleared=status_message is None,
            has_expiry=status_expires_at is not None,
        )
        return user

    @staticmethod
    async def expire_status_messages(db: AsyncSession) -> int:
        """
        Clear status messages whose status_expires_at has passed.
        Returns number of users updated. Called periodically by a background job.
        """
        from sqlalchemy import update as sql_update
        now = datetime.now(timezone.utc)
        stmt = (
            sql_update(User)
            .where(
                User.status_expires_at.isnot(None),
                User.status_expires_at <= now,
            )
            .values(status_message=None, status_expires_at=None)
            .execution_options(synchronize_session=False)
        )
        result = await db.execute(stmt)
        await db.commit()
        return result.rowcount or 0

    # ── Contacts ─────────────────────────────────────────

    @staticmethod
    async def add_contact(
        db: AsyncSession,
        user_id: str,
        contact_id: str,
        nickname: str | None = None,
    ) -> Contact:
        if user_id == contact_id:
            raise ConflictError("Cannot add yourself as a contact")

        # Verify contact user exists
        await UserService.get_user(db, contact_id)

        # Check duplicate
        existing = await db.execute(
            select(Contact).where(
                Contact.user_id == user_id,
                Contact.contact_id == contact_id,
            )
        )
        if existing.scalar_one_or_none():
            raise ConflictError("Contact already exists")

        contact = Contact(
            user_id=user_id,
            contact_id=contact_id,
            nickname=nickname,
        )
        db.add(contact)
        await db.commit()
        await db.refresh(contact)
        logger.info("contact_added", user_id=user_id, contact_id=contact_id)
        return contact

    @staticmethod
    async def list_contacts(
        db: AsyncSession,
        user_id: str,
    ) -> list[Contact]:
        result = await db.execute(
            select(Contact)
            .where(Contact.user_id == user_id)
            .options(selectinload(Contact.contact_user))
            .order_by(Contact.created_at.desc())
        )
        return list(result.scalars().all())

    @staticmethod
    async def update_contact(
        db: AsyncSession,
        user_id: str,
        contact_record_id: str,
        **kwargs,
    ) -> Contact:
        """
        Update a contact record.
        contact_record_id is the Contact table primary key (UUID).
        Falls back to matching by Contact.contact_id for backwards compatibility.
        """
        # Try by primary key first (preferred)
        result = await db.execute(
            select(Contact).where(
                Contact.user_id == user_id,
                Contact.id == contact_record_id,
            )
        )
        contact = result.scalar_one_or_none()

        # Fallback: match by contact_id (the related user's ID)
        if not contact:
            result = await db.execute(
                select(Contact).where(
                    Contact.user_id == user_id,
                    Contact.contact_id == contact_record_id,
                )
            )
            contact = result.scalar_one_or_none()

        if not contact:
            raise NotFoundError("Contact", contact_record_id)

        for key, value in kwargs.items():
            if value is not None and hasattr(contact, key):
                setattr(contact, key, value)
        await db.commit()
        await db.refresh(contact)
        return contact

    # ── Blocking helpers ─────────────────────────────────

    @staticmethod
    async def is_blocked_either_way(
        db: AsyncSession, user_a: str, user_b: str
    ) -> tuple[bool, str | None]:
        """
        Returns (blocked, blocker_id) where blocker_id is the user that blocked
        the other (or None when nobody is blocking). True if EITHER user has the
        other blocked.
        """
        if user_a == user_b:
            return (False, None)
        result = await db.execute(
            select(Contact).where(
                Contact.is_blocked == True,
                or_(
                    (Contact.user_id == user_a) & (Contact.contact_id == user_b),
                    (Contact.user_id == user_b) & (Contact.contact_id == user_a),
                ),
            )
        )
        rows = list(result.scalars().all())
        if not rows:
            return (False, None)
        # Return the first blocker found
        return (True, rows[0].user_id)

    @staticmethod
    async def has_blocked(
        db: AsyncSession, blocker_id: str, target_id: str
    ) -> bool:
        """True if blocker_id has explicitly blocked target_id."""
        if blocker_id == target_id:
            return False
        result = await db.execute(
            select(Contact).where(
                Contact.user_id == blocker_id,
                Contact.contact_id == target_id,
                Contact.is_blocked == True,
            )
        )
        return result.scalar_one_or_none() is not None

    @staticmethod
    async def get_blocked_user_ids(
        db: AsyncSession, user_id: str
    ) -> set[str]:
        """Return the set of user IDs that `user_id` has blocked."""
        result = await db.execute(
            select(Contact.contact_id).where(
                Contact.user_id == user_id,
                Contact.is_blocked == True,
            )
        )
        return set(row[0] for row in result.all())

    @staticmethod
    async def get_blockers_of(
        db: AsyncSession, user_id: str
    ) -> set[str]:
        """Return the set of user IDs that have blocked `user_id`."""
        result = await db.execute(
            select(Contact.user_id).where(
                Contact.contact_id == user_id,
                Contact.is_blocked == True,
            )
        )
        return set(row[0] for row in result.all())

    @staticmethod
    async def remove_contact(
        db: AsyncSession,
        user_id: str,
        contact_record_id: str,
    ) -> None:
        """
        Remove a contact record.
        contact_record_id is the Contact table primary key (UUID).
        Falls back to matching by Contact.contact_id for backwards compatibility.
        """
        # Try by primary key first (preferred)
        result = await db.execute(
            select(Contact).where(
                Contact.user_id == user_id,
                Contact.id == contact_record_id,
            )
        )
        contact = result.scalar_one_or_none()

        # Fallback: match by contact_id (the related user's ID)
        if not contact:
            result = await db.execute(
                select(Contact).where(
                    Contact.user_id == user_id,
                    Contact.contact_id == contact_record_id,
                )
            )
            contact = result.scalar_one_or_none()

        if not contact:
            raise NotFoundError("Contact", contact_record_id)
        await db.delete(contact)
        await db.commit()
        logger.info("contact_removed", user_id=user_id, contact_record_id=contact_record_id)
