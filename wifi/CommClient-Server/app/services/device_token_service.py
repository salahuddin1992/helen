"""
Device token service — register, list, deactivate device push tokens.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.models.device_token import DeviceToken

logger = get_logger(__name__)

_VALID_PROVIDERS = {"fcm", "apns", "web"}
_VALID_PLATFORMS = {"ios", "android", "web", "desktop"}


class DeviceTokenService:
    """CRUD for push notification device tokens."""

    @staticmethod
    async def register(
        db: AsyncSession,
        user_id: str,
        provider: str,
        token: str,
        platform: str,
        device_name: str | None = None,
        app_version: str | None = None,
        bundle_id: str | None = None,
        extra_json: str | None = None,
    ) -> DeviceToken:
        if provider not in _VALID_PROVIDERS:
            raise ValidationError(f"Unsupported push provider: {provider}")
        if platform not in _VALID_PLATFORMS:
            raise ValidationError(f"Unsupported platform: {platform}")
        if not token or len(token) > 512:
            raise ValidationError("Invalid push token")

        # Upsert: a (provider, token) pair is globally unique. If it exists,
        # claim it for this user (handles user-switching on a shared device).
        existing_q = await db.execute(
            select(DeviceToken).where(
                DeviceToken.provider == provider,
                DeviceToken.token == token,
            )
        )
        record = existing_q.scalar_one_or_none()

        if record is None:
            record = DeviceToken(
                user_id=user_id,
                provider=provider,
                token=token,
                platform=platform,
                device_name=device_name,
                app_version=app_version,
                bundle_id=bundle_id,
                extra_json=extra_json,
                is_active=True,
                failure_count=0,
            )
            db.add(record)
        else:
            record.user_id = user_id
            record.platform = platform
            record.device_name = device_name
            record.app_version = app_version
            record.bundle_id = bundle_id
            record.extra_json = extra_json
            record.is_active = True
            record.failure_count = 0
            record.last_error = None

        await db.commit()
        await db.refresh(record)
        logger.info(
            "device_token_registered",
            user_id=user_id,
            provider=provider,
            platform=platform,
            token_id=record.id,
        )
        return record

    @staticmethod
    async def list_for_user(db: AsyncSession, user_id: str) -> list[DeviceToken]:
        result = await db.execute(
            select(DeviceToken)
            .where(DeviceToken.user_id == user_id)
            .order_by(DeviceToken.created_at.desc())
        )
        return list(result.scalars().all())

    @staticmethod
    async def deactivate(db: AsyncSession, user_id: str, token_id: str) -> bool:
        result = await db.execute(
            select(DeviceToken).where(
                and_(
                    DeviceToken.id == token_id,
                    DeviceToken.user_id == user_id,
                )
            )
        )
        record = result.scalar_one_or_none()
        if record is None:
            raise NotFoundError("DeviceToken", token_id)
        record.is_active = False
        await db.commit()
        return True

    @staticmethod
    async def deactivate_by_token(
        db: AsyncSession, user_id: str, provider: str, token: str
    ) -> bool:
        """Deactivate using the raw push token (used when a client logs out)."""
        result = await db.execute(
            select(DeviceToken).where(
                and_(
                    DeviceToken.user_id == user_id,
                    DeviceToken.provider == provider,
                    DeviceToken.token == token,
                )
            )
        )
        record = result.scalar_one_or_none()
        if record is None:
            return False
        record.is_active = False
        await db.commit()
        return True

    @staticmethod
    async def touch(db: AsyncSession, token_id: str) -> None:
        """Update last_used_at — called by the dispatcher on success."""
        result = await db.execute(
            select(DeviceToken).where(DeviceToken.id == token_id)
        )
        record = result.scalar_one_or_none()
        if record is not None:
            record.last_used_at = datetime.now(timezone.utc)
            await db.commit()
