"""
Custom emoji REST endpoints.

Public:
  GET  /api/custom-emoji              — list every uploaded shortcode
  GET  /api/custom-emoji/{id}/raw     — fetch the asset bytes

Admin:
  POST /api/custom-emoji              — upload a new shortcode
  DELETE /api/custom-emoji/{id}       — remove one

Listing is public (any logged-in user) so the client picker can
populate. Upload + delete are admin-only — that mirrors how
Discord/Slack scope custom emoji on a community level.
"""

from __future__ import annotations

from fastapi import (
    APIRouter, Depends, HTTPException, UploadFile, File, Form, status,
)
from fastapi.responses import FileResponse

from app.core.deps import get_current_user_id
from app.core.security_utils import require_role
from app.services.custom_emoji_service import (
    CustomEmojiError, list_emoji, get_emoji, get_emoji_path,
    upload_emoji, delete_emoji,
)


router = APIRouter(prefix="/custom-emoji", tags=["custom-emoji"])


@router.get("")
async def list_custom_emoji(
    user_id: str = Depends(get_current_user_id),
):
    return {"emoji": [e.to_dict() for e in list_emoji()]}


@router.get("/{emoji_id}/raw")
async def get_custom_emoji_raw(
    emoji_id: str,
    user_id: str = Depends(get_current_user_id),
):
    e = get_emoji(emoji_id)
    if e is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="not found",
        )
    path = get_emoji_path(emoji_id)
    if path is None or not path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="asset missing",
        )
    return FileResponse(
        str(path),
        media_type=e.mime,
        # Long cache — emoji are immutable per id (delete makes a
        # new one with a new id).
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.post("", status_code=201)
async def upload_custom_emoji(
    shortcode: str = Form(..., min_length=2, max_length=32),
    description: str = Form("", max_length=200),
    file: UploadFile = File(...),
    user_id: str = Depends(require_role("admin")),
):
    body = await file.read()
    try:
        e = upload_emoji(
            shortcode=shortcode,
            mime=file.content_type or "application/octet-stream",
            body_bytes=body,
            uploaded_by=user_id,
            description=description,
        )
    except CustomEmojiError as ex:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(ex),
        )
    return e.to_dict()


@router.delete("/{emoji_id}", status_code=204)
async def delete_custom_emoji(
    emoji_id: str,
    user_id: str = Depends(require_role("admin")),
):
    ok = delete_emoji(emoji_id)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="not found",
        )
    return None


__all__ = ["router"]
