"""
Notification service — create, retrieve, mark read, and manage user notifications.
Uses async database sessions and structured logging.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.notification import Notification

logger = get_logger(__name__)


async def _fire_push(
    db: AsyncSession,
    user_ids: list[str],
    *,
    notification_type: str,
    title: str,
    body: str | None,
    reference_id: str | None,
    reference_type: str | None,
) -> None:
    """Best-effort push fan-out — never raises into the caller."""
    if not user_ids:
        return
    try:
        from app.services.push.dispatcher import push_dispatcher
        from app.services.push.provider import PushPayload

        payload = PushPayload(
            title=title,
            body=body,
            data={
                "type": notification_type,
                **({"reference_id": reference_id} if reference_id else {}),
                **({"reference_type": reference_type} if reference_type else {}),
            },
        )
        await push_dispatcher.dispatch_bulk(db, user_ids, payload)
    except Exception as e:
        logger.warning("push_dispatch_failed", error=str(e), type=notification_type)


class NotificationService:
    """Service for notification CRUD and bulk operations."""

    @staticmethod
    async def create_notification(
        db: AsyncSession,
        user_id: str,
        type: str,
        title: str,
        body: str | None = None,
        reference_id: str | None = None,
        reference_type: str | None = None,
    ) -> Notification:
        """
        Create a single notification for a user.

        Args:
            db: Async database session
            user_id: Target user ID
            type: Notification type (message, call_missed, etc.)
            title: Short notification title
            body: Optional longer description
            reference_id: ID of related entity
            reference_type: Type of related entity

        Returns:
            Created Notification model instance

        Raises:
            SQLAlchemy exceptions on DB errors
        """
        notification = Notification(
            user_id=user_id,
            type=type,
            title=title,
            body=body,
            reference_id=reference_id,
            reference_type=reference_type,
        )
        db.add(notification)
        await db.commit()
        await db.refresh(notification)
        logger.info(
            "notification_created",
            notification_id=notification.id,
            user_id=user_id,
            type=type,
        )
        await _fire_push(
            db,
            [user_id],
            notification_type=type,
            title=title,
            body=body,
            reference_id=reference_id,
            reference_type=reference_type,
        )
        return notification

    @staticmethod
    async def create_bulk(
        db: AsyncSession,
        user_ids: list[str],
        type: str,
        title: str,
        body: str | None = None,
        reference_id: str | None = None,
        reference_type: str | None = None,
    ) -> list[Notification]:
        """
        Create notifications for multiple users (group notifications).

        Args:
            db: Async database session
            user_ids: List of target user IDs
            type: Notification type
            title: Short notification title
            body: Optional longer description
            reference_id: ID of related entity
            reference_type: Type of related entity

        Returns:
            List of created Notification instances

        Raises:
            SQLAlchemy exceptions on DB errors
        """
        if not user_ids:
            return []

        notifications = [
            Notification(
                user_id=uid,
                type=type,
                title=title,
                body=body,
                reference_id=reference_id,
                reference_type=reference_type,
            )
            for uid in user_ids
        ]
        db.add_all(notifications)
        await db.commit()

        # Refresh all instances
        for notif in notifications:
            await db.refresh(notif)

        logger.info(
            "notifications_bulk_created",
            count=len(notifications),
            type=type,
            reference_id=reference_id,
        )
        await _fire_push(
            db,
            list(user_ids),
            notification_type=type,
            title=title,
            body=body,
            reference_id=reference_id,
            reference_type=reference_type,
        )
        return notifications

    @staticmethod
    async def get_user_notifications(
        db: AsyncSession,
        user_id: str,
        limit: int = 50,
        offset: int = 0,
        unread_only: bool = False,
    ) -> tuple[list[Notification], int, int]:
        """
        Fetch notifications for a user with pagination and filtering.

        Args:
            db: Async database session
            user_id: User ID to fetch notifications for
            limit: Max results per page (1-100)
            offset: Number of results to skip
            unread_only: If True, only return unread notifications

        Returns:
            Tuple of (notifications list, total count, unread count)
        """
        # Ensure limit is reasonable
        limit = max(1, min(limit, 100))
        offset = max(0, offset)

        # Build base query
        base_filter = [Notification.user_id == user_id]
        if unread_only:
            base_filter.append(Notification.is_read == False)

        # Count total (for pagination)
        total_result = await db.execute(
            select(func.count()).select_from(Notification).where(and_(*base_filter))
        )
        total = total_result.scalar() or 0

        # Count unread
        unread_result = await db.execute(
            select(func.count())
            .select_from(Notification)
            .where(
                and_(
                    Notification.user_id == user_id,
                    Notification.is_read == False,
                )
            )
        )
        unread_count = unread_result.scalar() or 0

        # Fetch paginated results, ordered newest first
        result = await db.execute(
            select(Notification)
            .where(and_(*base_filter))
            .order_by(Notification.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        notifications = list(result.scalars().all())

        logger.info(
            "notifications_fetched",
            user_id=user_id,
            limit=limit,
            offset=offset,
            unread_only=unread_only,
            count=len(notifications),
            total=total,
            unread=unread_count,
        )
        return notifications, total, unread_count

    @staticmethod
    async def mark_read(
        db: AsyncSession,
        user_id: str,
        notification_ids: list[str],
    ) -> int:
        """
        Mark specific notifications as read for a user.

        Args:
            db: Async database session
            user_id: User ID (for security — only their own notifications)
            notification_ids: List of notification IDs to mark read

        Returns:
            Number of notifications actually marked read

        Raises:
            SQLAlchemy exceptions on DB errors
        """
        if not notification_ids:
            return 0

        # Ensure user can only mark their own notifications
        now = datetime.now(timezone.utc)
        result = await db.execute(
            select(Notification)
            .where(
                and_(
                    Notification.user_id == user_id,
                    Notification.id.in_(notification_ids),
                    Notification.is_read == False,
                )
            )
        )
        notifications = list(result.scalars().all())
        count = len(notifications)

        for notif in notifications:
            notif.is_read = True
            notif.read_at = now

        if notifications:
            await db.commit()
            logger.info(
                "notifications_marked_read",
                user_id=user_id,
                count=count,
                notification_ids=notification_ids,
            )

        return count

    @staticmethod
    async def mark_all_read(db: AsyncSession, user_id: str) -> int:
        """
        Mark all unread notifications as read for a user.

        Args:
            db: Async database session
            user_id: User ID

        Returns:
            Number of notifications marked read

        Raises:
            SQLAlchemy exceptions on DB errors
        """
        now = datetime.now(timezone.utc)
        result = await db.execute(
            select(Notification).where(
                and_(
                    Notification.user_id == user_id,
                    Notification.is_read == False,
                )
            )
        )
        notifications = list(result.scalars().all())
        count = len(notifications)

        for notif in notifications:
            notif.is_read = True
            notif.read_at = now

        if notifications:
            await db.commit()
            logger.info(
                "notifications_all_marked_read",
                user_id=user_id,
                count=count,
            )

        return count

    @staticmethod
    async def delete_notification(
        db: AsyncSession,
        user_id: str,
        notification_id: str,
    ) -> bool:
        """
        Delete a single notification (security: verify ownership).

        Args:
            db: Async database session
            user_id: User ID (owner)
            notification_id: Notification ID to delete

        Returns:
            True if deleted, False if not found

        Raises:
            SQLAlchemy exceptions on DB errors
        """
        result = await db.execute(
            select(Notification).where(
                and_(
                    Notification.id == notification_id,
                    Notification.user_id == user_id,
                )
            )
        )
        notification = result.scalar_one_or_none()
        if not notification:
            return False

        await db.delete(notification)
        await db.commit()
        logger.info(
            "notification_deleted",
            notification_id=notification_id,
            user_id=user_id,
        )
        return True

    @staticmethod
    async def delete_old(db: AsyncSession, days: int = 30) -> int:
        """
        Delete notifications older than specified days (maintenance task).
        Useful for cleanup of old read notifications.

        Args:
            db: Async database session
            days: Delete notifications older than this many days

        Returns:
            Number of notifications deleted

        Raises:
            SQLAlchemy exceptions on DB errors
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        stmt = delete(Notification).where(
            and_(
                Notification.is_read == True,
                Notification.created_at < cutoff,
            )
        )
        result = await db.execute(stmt)
        await db.commit()
        count = result.rowcount or 0
        logger.info("old_notifications_deleted", count=count, days=days)
        return count

    @staticmethod
    async def get_unread_count(db: AsyncSession, user_id: str) -> int:
        """
        Get unread notification count for a user.

        Args:
            db: Async database session
            user_id: User ID

        Returns:
            Count of unread notifications
        """
        result = await db.execute(
            select(func.count()).select_from(Notification).where(
                and_(
                    Notification.user_id == user_id,
                    Notification.is_read == False,
                )
            )
        )
        count = result.scalar() or 0
        return count


# Singleton instance
notification_service = NotificationService()
