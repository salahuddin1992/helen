"""
Profile photos — Telegram-style multi-photo gallery with per-photo visibility.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import FileResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user_id, get_db
from app.schemas.profile_photo import (
    ProfilePhotoListResponse,
    ProfilePhotoResponse,
    ProfilePhotoUpdate,
)
from app.services.profile_photo_service import (
    ProfilePhotoService,
    build_photo_response,
)

router = APIRouter(prefix="/users", tags=["profile-photos"])


# ── Broadcast helper ────────────────────────────────────────

async def _broadcast_photos_updated(user_id: str, reason: str) -> None:
    """Emit a socket event so connected clients refresh this user's gallery."""
    try:
        from app.socket.server import sio
        await sio.emit(
            "user.photos_updated",
            {"user_id": user_id, "reason": reason},
        )
    except Exception:  # best-effort — never fail the HTTP call on bus errors
        pass


# ── Own photos ──────────────────────────────────────────────

@router.get("/me/photos", response_model=ProfilePhotoListResponse)
async def list_my_photos(
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    photos = await ProfilePhotoService.list_my_photos(db, user_id)
    return ProfilePhotoListResponse(
        photos=[ProfilePhotoResponse.model_validate(build_photo_response(p)) for p in photos],
        total=len(photos),
    )


@router.post(
    "/me/photos",
    response_model=ProfilePhotoResponse,
    status_code=201,
)
async def upload_my_photo(
    file: UploadFile = File(...),
    visibility: str = Form("public"),
    caption: str | None = Form(None),
    make_primary: bool = Form(False),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    photo = await ProfilePhotoService.upload(
        db,
        user_id=user_id,
        file=file,
        visibility=visibility,  # type: ignore[arg-type]
        caption=caption,
        make_primary=make_primary,
    )
    await _broadcast_photos_updated(user_id, "added")
    return ProfilePhotoResponse.model_validate(build_photo_response(photo))


@router.patch("/me/photos/{photo_id}", response_model=ProfilePhotoResponse)
async def update_my_photo(
    photo_id: str,
    body: ProfilePhotoUpdate,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    photo = await ProfilePhotoService.update(
        db,
        user_id=user_id,
        photo_id=photo_id,
        **body.model_dump(exclude_unset=True),
    )
    await _broadcast_photos_updated(user_id, "updated")
    return ProfilePhotoResponse.model_validate(build_photo_response(photo))


@router.delete("/me/photos/{photo_id}", status_code=204, response_class=Response)
async def delete_my_photo(
    photo_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> Response:
    await ProfilePhotoService.delete(db, user_id=user_id, photo_id=photo_id)
    await _broadcast_photos_updated(user_id, "deleted")
    return Response(status_code=204)


# ── Viewing someone else's gallery ──────────────────────────

@router.get("/{target_id}/photos", response_model=ProfilePhotoListResponse)
async def list_user_photos(
    target_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    photos = await ProfilePhotoService.list_visible_for_viewer(
        db, owner_id=target_id, viewer_id=user_id
    )
    return ProfilePhotoListResponse(
        photos=[ProfilePhotoResponse.model_validate(build_photo_response(p)) for p in photos],
        total=len(photos),
    )


@router.get("/{target_id}/photos/{photo_id}/image")
async def get_photo_binary(
    target_id: str,
    photo_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    photo = await ProfilePhotoService.get_photo_for_viewer(
        db, owner_id=target_id, photo_id=photo_id, viewer_id=user_id
    )
    path = ProfilePhotoService.resolve_path(photo)
    if not path.exists():
        # Row exists but file is gone — surface as 404 to the client.
        return Response(status_code=404)
    return FileResponse(
        str(path),
        media_type=photo.mime_type,
        headers={"Cache-Control": "private, max-age=3600"},
    )
