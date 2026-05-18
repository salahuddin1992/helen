"""
User and contacts REST endpoints.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user_id, get_db
from app.core.share_code import is_valid_share_code
from app.models.user import User
from app.schemas.user import (
    ContactCreate,
    ContactResponse,
    ContactUpdate,
    StatusMessageUpdate,
    UserListResponse,
    UserProfile,
    UserUpdate,
)
from app.services.user_service import UserService

router = APIRouter(prefix="/users", tags=["users"])


# ── User Profile ─────────────────────────────────────────

@router.get("", response_model=UserListResponse)
async def list_users(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    search: str | None = Query(None, max_length=128),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    users, total = await UserService.list_users(db, skip=skip, limit=limit, search=search)
    return UserListResponse(
        users=[UserProfile.model_validate(u) for u in users],
        total=total,
    )


@router.get("/me", response_model=UserProfile)
async def get_current_user(
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    user = await UserService.get_user(db, user_id)
    return UserProfile.model_validate(user)


@router.patch("/me", response_model=UserProfile)
async def update_current_user(
    body: UserUpdate,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    user = await UserService.update_user(
        db, user_id, **body.model_dump(exclude_unset=True),
    )
    return UserProfile.model_validate(user)


@router.put("/me/status-message", response_model=UserProfile)
async def set_status_message(
    body: StatusMessageUpdate,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """Set or clear the user's custom status message (e.g. 'In a meeting')."""
    user = await UserService.set_status_message(
        db, user_id,
        status_message=body.status_message,
        status_expires_at=body.status_expires_at,
    )
    # Best-effort broadcast over Socket.IO so other clients see the change live
    try:
        from app.socket.server import sio
        await sio.emit(
            "presence:status_message_changed",
            {
                "user_id": user_id,
                "status_message": user.status_message,
                "status_expires_at": user.status_expires_at.isoformat() if user.status_expires_at else None,
            },
        )
    except Exception:
        pass
    return UserProfile.model_validate(user)


@router.delete("/me/status-message", response_model=UserProfile)
async def clear_status_message(
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """Clear the user's custom status message."""
    user = await UserService.set_status_message(db, user_id, status_message=None)
    try:
        from app.socket.server import sio
        await sio.emit(
            "presence:status_message_changed",
            {"user_id": user_id, "status_message": None, "status_expires_at": None},
        )
    except Exception:
        pass
    return UserProfile.model_validate(user)


@router.get("/by-code/{code}")
async def get_user_by_share_code(
    code: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """Resolve a public share_code to a user profile.

    The code is the 64-char alphanumeric token each user can hand out so
    peers can find them without knowing their UUID or username.

    Lookup order:
      1. Local users table (fast path).
      2. When federation is enabled and the code isn't here, fan out to
         every live LAN peer. A match returns the user with an
         `origin_server` block so the client knows where follow-up
         requests should be sent.
    """
    if not is_valid_share_code(code):
        raise HTTPException(status_code=400, detail="Invalid share code format")

    # 1. Local lookup
    result = await db.execute(select(User).where(User.share_code == code))
    user = result.scalar_one_or_none()
    if user and user.is_active:
        local = UserProfile.model_validate(user).model_dump(mode="json")
        local["origin_server"] = None  # local-to-this-server
        return local

    # 2. Federated fallback
    from app.core.config import get_settings
    if get_settings().FEDERATION_ENABLED:
        from app.services.federation_service import federation_service
        remote = await federation_service.lookup_user_by_code(code)
        if remote is not None:
            return remote.to_dict()

    raise HTTPException(status_code=404, detail="No user with that code")


@router.get("/{target_id}", response_model=UserProfile)
async def get_user(
    target_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    user = await UserService.get_user(db, target_id)
    return UserProfile.model_validate(user)


# ── Contacts ─────────────────────────────────────────────

@router.get("/me/contacts", response_model=list[ContactResponse])
async def list_contacts(
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    contacts = await UserService.list_contacts(db, user_id)
    results = []
    for c in contacts:
        results.append(ContactResponse(
            id=c.id,
            contact=UserProfile.model_validate(c.contact_user),
            nickname=c.nickname,
            is_blocked=c.is_blocked,
            is_favorite=c.is_favorite,
            created_at=c.created_at,
        ))
    return results


@router.post("/me/contacts", response_model=ContactResponse, status_code=201)
async def add_contact(
    body: ContactCreate,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    contact = await UserService.add_contact(
        db, user_id, body.contact_id, body.nickname,
    )
    # Reload with relationship
    contacts = await UserService.list_contacts(db, user_id)
    c = next(c for c in contacts if c.contact_id == body.contact_id)
    return ContactResponse(
        id=c.id,
        contact=UserProfile.model_validate(c.contact_user),
        nickname=c.nickname,
        is_blocked=c.is_blocked,
        is_favorite=c.is_favorite,
        created_at=c.created_at,
    )


@router.patch("/me/contacts/{contact_id}")
async def update_contact(
    contact_id: str,
    body: ContactUpdate,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    await UserService.update_contact(
        db, user_id, contact_id, **body.model_dump(exclude_unset=True),
    )
    return {"status": "updated"}


@router.delete("/me/contacts/{contact_id}", status_code=204, response_class=Response)
async def remove_contact(
    contact_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> Response:
    await UserService.remove_contact(db, user_id, contact_id)
    return Response(status_code=204)
