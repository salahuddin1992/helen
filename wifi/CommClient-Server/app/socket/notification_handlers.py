"""
Notification socket event handlers — real-time notification delivery and marking.
Emits notifications to all connected sockets for a user.
"""

from __future__ import annotations

from app.core.logging import get_logger
from app.db.session import async_session_factory
from app.services.notification_service import notification_service
from app.services.presence_service import presence_service
from app.socket.server import emit_to_user, get_user_id, sio

logger = get_logger(__name__)


@sio.event
async def notification_mark_read(sid: str, data: dict):
    """
    Client marks notifications as read via socket.

    Client Data:
    {
        notification_ids: list[str]  # IDs to mark read
    }

    Emits back to client:
    - notification:read_ack with {success: bool, marked_count: int, unread_count: int}
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return {"error": "Not authenticated"}

    notification_ids = data.get("notification_ids", [])
    if not notification_ids or not isinstance(notification_ids, list):
        await sio.emit(
            "notification:read_ack",
            {
                "success": False,
                "error": "Invalid notification_ids",
            },
            to=sid,
        )
        return

    try:
        async with async_session_factory() as db:
            marked_count = await notification_service.mark_read(
                db,
                user_id,
                notification_ids,
            )
            unread_count = await notification_service.get_unread_count(db, user_id)

            await sio.emit(
                "notification:read_ack",
                {
                    "success": True,
                    "marked_count": marked_count,
                    "unread_count": unread_count,
                },
                to=sid,
            )

            logger.info(
                "socket_notification_marked_read",
                user_id=user_id,
                sid=sid,
                marked_count=marked_count,
            )

    except Exception as e:
        logger.error(
            "socket_notification_mark_read_error",
            user_id=user_id,
            sid=sid,
            error=str(e),
        )
        await sio.emit(
            "notification:read_ack",
            {
                "success": False,
                "error": "Failed to mark notifications read",
            },
            to=sid,
        )


async def emit_notification(
    user_id: str,
    notification_data: dict,
) -> int:
    """
    Emit notification to all connected sockets for a user.

    This is the main helper function for emitting real-time notifications
    to a user across all their connections.

    Args:
        user_id: User to receive notification
        notification_data: Notification data dict with keys:
            - id: Notification ID
            - type: Notification type (message, call_missed, etc.)
            - title: Notification title
            - body: Optional body text
            - reference_id: Optional related entity ID
            - reference_type: Optional related entity type
            - is_read: Boolean
            - read_at: Optional datetime
            - created_at: Datetime

    Returns:
        Number of sockets the notification was emitted to

    Example:
        await emit_notification(user_id, {
            "id": "abc123...",
            "type": "message",
            "title": "New message from Alice",
            "body": "Hey, how are you?",
            "reference_id": msg_id,
            "reference_type": "message",
            "is_read": False,
            "read_at": None,
            "created_at": datetime.now(timezone.utc),
        })
    """
    # emit_to_user fans out to every local sid AND falls back to
    # federation when the user is on a sibling Helen server. Returns
    # the actual delivery count (0 means user fully offline across the
    # cluster).
    try:
        count = await emit_to_user(
            "notification:new", notification_data, user_id,
        )
    except Exception as e:
        logger.error(
            "emit_notification_error",
            user_id=user_id,
            notification_type=notification_data.get("type"),
            error=str(e),
        )
        return 0

    if count == 0:
        logger.info(
            "emit_notification_no_sockets",
            user_id=user_id,
            notification_type=notification_data.get("type"),
        )
        return 0

    logger.info(
        "notification_emitted",
        user_id=user_id,
        socket_count=count,
        notification_type=notification_data.get("type"),
    )
    return count


async def create_and_emit_notification(
    user_id: str,
    notification_type: str,
    title: str,
    body: str | None = None,
    reference_id: str | None = None,
    reference_type: str | None = None,
) -> tuple[dict, int]:
    """
    Create a notification in the database and emit it in real-time to the user.

    Convenience wrapper that combines create_notification + emit_notification.

    Args:
        user_id: User to receive notification
        notification_type: Type of notification
        title: Short title
        body: Optional longer description
        reference_id: Optional related entity ID
        reference_type: Optional related entity type

    Returns:
        Tuple of (notification_data dict, sockets_emitted count)

    Example:
        notif_data, emitted = await create_and_emit_notification(
            user_id="user123",
            notification_type="message",
            title="New message from Alice",
            body="Hey, how are you?",
            reference_id=msg_id,
            reference_type="message",
        )
    """
    try:
        async with async_session_factory() as db:
            notification = await notification_service.create_notification(
                db,
                user_id,
                notification_type,
                title,
                body,
                reference_id,
                reference_type,
            )

            # Build response data for emission
            notification_data = {
                "id": notification.id,
                "type": notification.type,
                "title": notification.title,
                "body": notification.body,
                "reference_id": notification.reference_id,
                "reference_type": notification.reference_type,
                "is_read": notification.is_read,
                "read_at": notification.read_at.isoformat() if notification.read_at else None,
                "created_at": notification.created_at.isoformat(),
            }

            # Emit in real-time
            emitted = await emit_notification(user_id, notification_data)

            return notification_data, emitted

    except Exception as e:
        logger.error(
            "create_and_emit_notification_error",
            user_id=user_id,
            notification_type=notification_type,
            error=str(e),
        )
        raise
