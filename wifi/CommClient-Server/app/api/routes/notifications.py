"""
Notification REST endpoints — list, mark read, delete.
Requires authentication via Bearer token.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user_id, get_db
from app.core.logging import get_logger
from app.schemas.notification import (
    MarkReadRequest,
    NotificationListResponse,
    NotificationResponse,
)
from app.services.notification_service import notification_service

logger = get_logger(__name__)

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("", response_model=NotificationListResponse)
async def list_notifications(
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    unread_only: bool = Query(False),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """
    List user's notifications with optional filtering.

    Query Parameters:
    - limit: Number of results (1-100, default 50)
    - offset: Number to skip for pagination
    - unread_only: If true, only return unread notifications

    Returns:
    - notifications: List of NotificationResponse
    - total: Total notification count for this user
    - unread_count: Total unread notification count
    """
    notifications, total, unread_count = await notification_service.get_user_notifications(
        db,
        user_id,
        limit=limit,
        offset=offset,
        unread_only=unread_only,
    )

    return NotificationListResponse(
        notifications=[
            NotificationResponse.model_validate(n) for n in notifications
        ],
        total=total,
        unread_count=unread_count,
    )


@router.get("/count", response_model=dict)
async def get_unread_count(
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """
    Get unread notification count for current user.

    Returns:
    - unread_count: Number of unread notifications
    """
    unread_count = await notification_service.get_unread_count(db, user_id)
    return {"unread_count": unread_count}


@router.post("/mark-read", response_model=dict)
async def mark_notifications_read(
    request: MarkReadRequest,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """
    Mark specific notifications as read.

    Request Body:
    - notification_ids: List of notification IDs to mark read (1-100)

    Returns:
    - marked_count: Number of notifications marked read
    - unread_count: Updated unread notification count
    """
    marked_count = await notification_service.mark_read(
        db,
        user_id,
        request.notification_ids,
    )

    unread_count = await notification_service.get_unread_count(db, user_id)

    return {
        "marked_count": marked_count,
        "unread_count": unread_count,
    }


@router.post("/mark-all-read", response_model=dict)
async def mark_all_read(
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """
    Mark all notifications as read for current user.

    Returns:
    - marked_count: Number of notifications marked read
    - unread_count: Updated unread notification count (will be 0)
    """
    marked_count = await notification_service.mark_all_read(db, user_id)
    unread_count = await notification_service.get_unread_count(db, user_id)

    return {
        "marked_count": marked_count,
        "unread_count": unread_count,
    }


@router.delete("/{notification_id}", response_model=dict)
async def delete_notification(
    notification_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """
    Delete a single notification.

    Path Parameters:
    - notification_id: ID of notification to delete

    Returns:
    - deleted: Boolean indicating success
    - message: Status message

    Raises:
    - 404: Notification not found or does not belong to user
    """
    deleted = await notification_service.delete_notification(
        db,
        user_id,
        notification_id,
    )

    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Notification not found",
        )

    return {
        "deleted": True,
        "message": "Notification deleted successfully",
    }
