"""
Profile photo service — multi-photo gallery with per-photo visibility.

Storage layout::

    <upload_path>/profile_photos/<user_id>/<photo_id>.<ext>

Visibility rules:
    public    — any authenticated user on this server
    contacts  — viewer must appear in the owner's contacts table (unidirectional)
    private   — only the owner
"""

from __future__ import annotations

import mimetypes
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import UploadFile
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import ForbiddenError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.models.contact import Contact
from app.models.profile_photo import ProfilePhoto
from app.models.user import User

logger = get_logger(__name__)

_ALLOWED_MIME_PREFIXES = ("image/",)
_ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "webp", "gif"}
_MAX_BYTES = 15 * 1024 * 1024  # 15 MB per photo
_PHOTO_DIR_NAME = "profile_photos"

Visibility = Literal["public", "contacts", "private"]


def _photos_root() -> Path:
    root = get_settings().upload_path / _PHOTO_DIR_NAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def _photo_dir(user_id: str) -> Path:
    d = _photos_root() / user_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_ext(filename: str, mime: str | None) -> str:
    name_ext = ""
    if "." in filename:
        name_ext = filename.rsplit(".", 1)[-1].lower()
    if name_ext in _ALLOWED_EXTENSIONS:
        return name_ext
    if mime:
        guessed = mimetypes.guess_extension(mime) or ""
        guessed = guessed.lstrip(".").lower()
        if guessed in _ALLOWED_EXTENSIONS:
            return guessed
    raise ValidationError("Unsupported image format")


def _photo_url(photo: ProfilePhoto) -> str:
    return f"/api/users/{photo.user_id}/photos/{photo.id}/image"


class ProfilePhotoService:

    # ── Permission helpers ──────────────────────────────────

    @staticmethod
    async def _viewer_can_see(
        db: AsyncSession, *, owner_id: str, viewer_id: str, visibility: str
    ) -> bool:
        if owner_id == viewer_id:
            return True
        if visibility == "public":
            return True
        if visibility == "private":
            return False
        if visibility == "contacts":
            # Unidirectional: viewer counts if the owner has added them.
            result = await db.execute(
                select(Contact.id).where(
                    Contact.user_id == owner_id,
                    Contact.contact_id == viewer_id,
                    Contact.is_blocked == False,  # noqa: E712
                )
            )
            return result.scalar_one_or_none() is not None
        return False

    # ── Queries ─────────────────────────────────────────────

    @staticmethod
    async def list_my_photos(
        db: AsyncSession, user_id: str
    ) -> list[ProfilePhoto]:
        result = await db.execute(
            select(ProfilePhoto)
            .where(ProfilePhoto.user_id == user_id)
            .order_by(ProfilePhoto.position.asc(), ProfilePhoto.created_at.desc())
        )
        return list(result.scalars().all())

    @staticmethod
    async def list_visible_for_viewer(
        db: AsyncSession, *, owner_id: str, viewer_id: str
    ) -> list[ProfilePhoto]:
        rows = await ProfilePhotoService.list_my_photos(db, owner_id)
        visible: list[ProfilePhoto] = []
        for p in rows:
            if await ProfilePhotoService._viewer_can_see(
                db, owner_id=owner_id, viewer_id=viewer_id, visibility=p.visibility
            ):
                visible.append(p)
        return visible

    @staticmethod
    async def get_photo_for_viewer(
        db: AsyncSession, *, owner_id: str, photo_id: str, viewer_id: str
    ) -> ProfilePhoto:
        result = await db.execute(
            select(ProfilePhoto).where(
                ProfilePhoto.id == photo_id,
                ProfilePhoto.user_id == owner_id,
            )
        )
        photo = result.scalar_one_or_none()
        if not photo:
            raise NotFoundError("ProfilePhoto", photo_id)
        if not await ProfilePhotoService._viewer_can_see(
            db,
            owner_id=owner_id,
            viewer_id=viewer_id,
            visibility=photo.visibility,
        ):
            raise ForbiddenError("Not allowed to view this photo")
        return photo

    @staticmethod
    def resolve_path(photo: ProfilePhoto) -> Path:
        return _photo_dir(photo.user_id) / photo.storage_name

    # ── Mutations ───────────────────────────────────────────

    @staticmethod
    async def upload(
        db: AsyncSession,
        *,
        user_id: str,
        file: UploadFile,
        visibility: Visibility = "public",
        caption: str | None = None,
        make_primary: bool = False,
    ) -> ProfilePhoto:
        if visibility not in ("public", "contacts", "private"):
            raise ValidationError("Invalid visibility")

        mime = (file.content_type or "").lower()
        if not any(mime.startswith(p) for p in _ALLOWED_MIME_PREFIXES):
            raise ValidationError("File must be an image")
        ext = _safe_ext(file.filename or "", mime)

        # Generate the row first so we can use its id in the filename.
        photo = ProfilePhoto(
            user_id=user_id,
            storage_name="",  # filled once we know the id
            mime_type=mime or "image/octet-stream",
            size_bytes=0,
            visibility=visibility,
            caption=caption,
            position=await ProfilePhotoService._next_position(db, user_id),
        )
        db.add(photo)
        await db.flush()

        photo.storage_name = f"{photo.id}.{ext}"
        target = _photo_dir(user_id) / photo.storage_name

        # Stream to disk with a hard size cap.
        written = 0
        with target.open("wb") as sink:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > _MAX_BYTES:
                    sink.close()
                    target.unlink(missing_ok=True)
                    await db.rollback()
                    raise ValidationError("Image too large (max 15 MB)")
                sink.write(chunk)
        photo.size_bytes = written

        if make_primary:
            await ProfilePhotoService._apply_primary(db, user_id, photo.id)
        else:
            # If this is the user's first photo, auto-promote it so avatar_url
            # gets populated.
            count_stmt = select(ProfilePhoto.id).where(
                ProfilePhoto.user_id == user_id,
            )
            rows = list((await db.execute(count_stmt)).scalars().all())
            if len(rows) == 1:
                await ProfilePhotoService._apply_primary(db, user_id, photo.id)

        await db.commit()
        await db.refresh(photo)
        logger.info(
            "profile_photo_uploaded",
            user_id=user_id,
            photo_id=photo.id,
            visibility=visibility,
            bytes=written,
        )
        return photo

    @staticmethod
    async def update(
        db: AsyncSession,
        *,
        user_id: str,
        photo_id: str,
        visibility: Visibility | None = None,
        is_primary: bool | None = None,
        caption: str | None = None,
        position: int | None = None,
    ) -> ProfilePhoto:
        result = await db.execute(
            select(ProfilePhoto).where(
                ProfilePhoto.id == photo_id,
                ProfilePhoto.user_id == user_id,
            )
        )
        photo = result.scalar_one_or_none()
        if not photo:
            raise NotFoundError("ProfilePhoto", photo_id)

        if visibility is not None:
            if visibility not in ("public", "contacts", "private"):
                raise ValidationError("Invalid visibility")
            photo.visibility = visibility
        if caption is not None:
            photo.caption = caption
        if position is not None:
            photo.position = position

        photo.updated_at = datetime.now(timezone.utc)

        if is_primary is True:
            await ProfilePhotoService._apply_primary(db, user_id, photo.id)

        await db.commit()
        await db.refresh(photo)
        return photo

    @staticmethod
    async def delete(
        db: AsyncSession, *, user_id: str, photo_id: str
    ) -> str | None:
        """
        Delete a photo. Returns the id of the new primary photo (if any), so
        callers can broadcast the change.
        """
        result = await db.execute(
            select(ProfilePhoto).where(
                ProfilePhoto.id == photo_id,
                ProfilePhoto.user_id == user_id,
            )
        )
        photo = result.scalar_one_or_none()
        if not photo:
            raise NotFoundError("ProfilePhoto", photo_id)

        was_primary = photo.is_primary
        path = ProfilePhotoService.resolve_path(photo)
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning(
                "profile_photo_unlink_failed",
                user_id=user_id,
                photo_id=photo_id,
                error=str(exc),
            )

        await db.delete(photo)
        await db.flush()

        new_primary_id: str | None = None
        if was_primary:
            remaining = await db.execute(
                select(ProfilePhoto)
                .where(ProfilePhoto.user_id == user_id)
                .order_by(ProfilePhoto.position.asc(), ProfilePhoto.created_at.desc())
                .limit(1)
            )
            next_photo = remaining.scalar_one_or_none()
            if next_photo:
                await ProfilePhotoService._apply_primary(db, user_id, next_photo.id)
                new_primary_id = next_photo.id
            else:
                # No photos left — clear the avatar.
                await db.execute(
                    update(User).where(User.id == user_id).values(avatar_url=None)
                )

        await db.commit()
        logger.info(
            "profile_photo_deleted",
            user_id=user_id,
            photo_id=photo_id,
            was_primary=was_primary,
            new_primary_id=new_primary_id,
        )
        return new_primary_id

    # ── Internals ───────────────────────────────────────────

    @staticmethod
    async def _next_position(db: AsyncSession, user_id: str) -> int:
        result = await db.execute(
            select(ProfilePhoto.position)
            .where(ProfilePhoto.user_id == user_id)
            .order_by(ProfilePhoto.position.desc())
            .limit(1)
        )
        top = result.scalar_one_or_none()
        return (top or 0) + 1 if top is not None else 0

    @staticmethod
    async def _apply_primary(
        db: AsyncSession, user_id: str, photo_id: str
    ) -> None:
        """Set the given photo as primary and mirror its URL into users.avatar_url."""
        # Clear any existing primary flag.
        await db.execute(
            update(ProfilePhoto)
            .where(
                ProfilePhoto.user_id == user_id,
                ProfilePhoto.id != photo_id,
            )
            .values(is_primary=False)
        )
        await db.execute(
            update(ProfilePhoto)
            .where(ProfilePhoto.id == photo_id)
            .values(is_primary=True)
        )
        # Mirror into users.avatar_url so existing consumers still work.
        url = f"/api/users/{user_id}/photos/{photo_id}/image"
        await db.execute(
            update(User).where(User.id == user_id).values(avatar_url=url)
        )


def build_photo_response(photo: ProfilePhoto) -> dict:
    return {
        "id": photo.id,
        "user_id": photo.user_id,
        "visibility": photo.visibility,
        "is_primary": photo.is_primary,
        "position": photo.position,
        "mime_type": photo.mime_type,
        "size_bytes": photo.size_bytes,
        "caption": photo.caption,
        "url": _photo_url(photo),
        "created_at": photo.created_at,
    }
