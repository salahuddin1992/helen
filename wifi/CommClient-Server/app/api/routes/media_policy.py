"""
Media policy REST API — admin-controlled resolution / framerate / bitrate
caps plus the client-facing `/me` endpoint that tells a user what cap
applies to them right now.

Endpoints
---------
Authenticated user:
  GET    /api/media-policy/me              — effective cap + ladder + active preset
  GET    /api/media-policy/presets         — quick-pick camera preset list
  GET    /api/media-policy/me/preset       — current user's preset
  PUT    /api/media-policy/me/preset       — switch to a preset (quick-pick)

Admin only:
  GET    /api/admin/media-policy           — full policy incl. role caps
  PATCH  /api/admin/media-policy           — update policy fields
  GET    /api/admin/media-policy/overrides — list every per-user override
  PUT    /api/admin/media-policy/overrides/{user_id}  — upsert override
  DELETE /api/admin/media-policy/overrides/{user_id}  — remove override
  GET    /api/admin/media-policy/presets   — list every preset (incl. disabled)
  POST   /api/admin/media-policy/presets   — create custom preset
  PATCH  /api/admin/media-policy/presets/{id} — edit preset
  DELETE /api/admin/media-policy/presets/{id} — delete custom preset
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_log
from app.core.deps import get_db
from app.core.logging import get_logger
from app.core.security_utils import require_role, security_scheme
from app.core.security import decode_token
from app.services.media_policy_service import (
    media_policy_service,
    get_resolution_ladder,
)
from fastapi.security import HTTPAuthorizationCredentials
from fastapi import Depends as _Depends  # noqa: F401

logger = get_logger(__name__)


# Two routers share a module: a user-facing one at /media-policy and
# an admin-only one at /admin/media-policy. Both are mounted by
# app.api.routes.__init__.
user_router = APIRouter(prefix="/media-policy", tags=["media-policy"])
admin_router = APIRouter(prefix="/admin/media-policy", tags=["admin", "media-policy"])


# ── Pydantic schemas ──────────────────────────────────────

class RoleCap(BaseModel):
    max_w: int = Field(..., ge=160, le=7680)
    max_h: int = Field(..., ge=120, le=4320)
    max_fps: int = Field(..., ge=1, le=120)
    max_kbps: int = Field(..., ge=0, le=200_000)


class PolicyUpdate(BaseModel):
    global_max_width: int | None = Field(default=None, ge=160, le=7680)
    global_max_height: int | None = Field(default=None, ge=120, le=4320)
    global_max_framerate: int | None = Field(default=None, ge=1, le=120)
    global_max_bitrate_kbps: int | None = Field(default=None, ge=0, le=200_000)
    allow_8k: bool | None = None
    allow_client_override: bool | None = None
    enforce_hard_cap: bool | None = None
    role_caps: dict[str, RoleCap] | None = None
    transcoding_enabled: bool | None = None
    prefer_hw_encoder: bool | None = None
    auto_max_quality: bool | None = None


class UserOverrideUpsert(BaseModel):
    max_width: int | None = Field(default=None, ge=0, le=7680)
    max_height: int | None = Field(default=None, ge=0, le=4320)
    max_framerate: int | None = Field(default=None, ge=0, le=120)
    max_bitrate_kbps: int | None = Field(default=None, ge=0, le=200_000)
    note: str | None = Field(default=None, max_length=500)


# Preset-specific shapes. Both admin-create and admin-patch share the same
# validation envelope; on create everything except `id`/`label` is optional
# and falls back to 720p-shaped defaults so admins can add a preset fast.
class PresetCreate(BaseModel):
    id: str = Field(..., min_length=1, max_length=64,
                    pattern=r"^[a-zA-Z0-9_\-]+$")
    label: str = Field(..., min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=200)
    width: int = Field(default=1280, ge=0, le=16_000)
    height: int = Field(default=720, ge=0, le=16_000)
    framerate: int = Field(default=30, ge=0, le=240)
    bitrate_kbps: int = Field(default=3000, ge=0, le=1_000_000)
    codec_preference: str = Field(default="auto", pattern=r"^(auto|h264|hevc|av1|vp8|vp9)$")
    requires_8k: bool = False
    enabled: bool = True
    is_default: bool = False
    sort_order: int = Field(default=100, ge=0, le=10_000)


class PresetPatch(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=200)
    width: int | None = Field(default=None, ge=0, le=16_000)
    height: int | None = Field(default=None, ge=0, le=16_000)
    framerate: int | None = Field(default=None, ge=0, le=240)
    bitrate_kbps: int | None = Field(default=None, ge=0, le=1_000_000)
    codec_preference: str | None = Field(default=None, pattern=r"^(auto|h264|hevc|av1|vp8|vp9)$")
    requires_8k: bool | None = None
    enabled: bool | None = None
    is_default: bool | None = None
    sort_order: int | None = Field(default=None, ge=0, le=10_000)


class UserPresetSelect(BaseModel):
    # `preset_id=None` clears the user's pick and falls back to the default.
    preset_id: str | None = Field(default=None, max_length=64)


# ── Helpers ───────────────────────────────────────────────

def _policy_to_dict(policy) -> dict[str, Any]:
    try:
        role_caps = json.loads(policy.role_caps_json or "{}")
    except (ValueError, TypeError):
        role_caps = {}
    return {
        "id": policy.id,
        "global_max_width": policy.global_max_width,
        "global_max_height": policy.global_max_height,
        "global_max_framerate": policy.global_max_framerate,
        "global_max_bitrate_kbps": policy.global_max_bitrate_kbps,
        "allow_8k": policy.allow_8k,
        "allow_client_override": policy.allow_client_override,
        "enforce_hard_cap": policy.enforce_hard_cap,
        "role_caps": role_caps,
        "transcoding_enabled": policy.transcoding_enabled,
        "prefer_hw_encoder": policy.prefer_hw_encoder,
        "auto_max_quality": policy.auto_max_quality,
        "updated_at": policy.updated_at.isoformat() if policy.updated_at else None,
    }


async def _require_authenticated_user(
    credentials: HTTPAuthorizationCredentials = Depends(security_scheme),
) -> str:
    """Minimal access-token guard — returns the user_id."""
    payload = decode_token(credentials.credentials)
    if payload.get("type") != "access":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token type",
        )
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token missing subject",
        )
    return user_id


# ── User-facing ──────────────────────────────────────────

@user_router.get("/me")
async def get_my_media_cap(
    user_id: str = Depends(_require_authenticated_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Return the effective cap + the allowed resolution ladder + the user's
    active camera preset (with clamped effective numbers). Called by the
    Electron client on login and before each call to populate the quality
    dropdown.
    """
    cap = await media_policy_service.effective_cap_for(db, user_id)
    ladder = [
        r for r in get_resolution_ladder(cap.allow_8k)
        if r["w"] <= cap.max_width
        and r["h"] <= cap.max_height
        and (r["fps"] <= cap.max_framerate or r["w"] == 0)
    ]
    active_preset = await media_policy_service.resolve_active_preset(db, user_id)
    return {
        "cap": cap.as_dict(),
        "ladder": ladder,
        "active_preset": (
            media_policy_service.preset_to_dict(active_preset, cap=cap)
            if active_preset else None
        ),
    }


@user_router.get("/presets")
async def list_my_presets(
    user_id: str = Depends(_require_authenticated_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Return every preset the user can pick from — enabled rows only, with
    each one annotated with `effective` (post-cap) and `available`
    (filtered against the user's 8K permission). The client renders this
    as a quick-pick dropdown and PUTs /me/preset to switch.
    """
    cap = await media_policy_service.effective_cap_for(db, user_id)
    presets = await media_policy_service.list_presets(db)
    return {
        "presets": [
            media_policy_service.preset_to_dict(p, cap=cap) for p in presets
        ],
    }


@user_router.get("/me/preset")
async def get_my_preset(
    user_id: str = Depends(_require_authenticated_user),
    db: AsyncSession = Depends(get_db),
):
    cap = await media_policy_service.effective_cap_for(db, user_id)
    preset = await media_policy_service.resolve_active_preset(db, user_id)
    if preset is None:
        return {"preset": None, "cap": cap.as_dict()}
    return {
        "preset": media_policy_service.preset_to_dict(preset, cap=cap),
        "cap": cap.as_dict(),
    }


@user_router.put("/me/preset")
async def set_my_preset(
    selection: UserPresetSelect,
    user_id: str = Depends(_require_authenticated_user),
    db: AsyncSession = Depends(get_db),
):
    """Quick-pick endpoint: switch the camera preset for this user.

    Body `{"preset_id": "4k"}` selects a preset. `{"preset_id": null}`
    clears the pick and falls back to the server default.
    """
    try:
        preset = await media_policy_service.set_user_active_preset(
            db, user_id, selection.preset_id,
        )
        await db.commit()
    except ValueError as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    except Exception as e:
        await db.rollback()
        logger.error("camera_preset_set_failed", user_id=user_id, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to set preset",
        )

    audit_log(
        "user.camera_preset_set",
        user_id=user_id,
        success=True,
        details={"preset_id": selection.preset_id or "default"},
    )
    cap = await media_policy_service.effective_cap_for(db, user_id)
    return {
        "preset": (
            media_policy_service.preset_to_dict(preset, cap=cap) if preset else None
        ),
    }


# ── Admin ────────────────────────────────────────────────

@admin_router.get("")
async def get_policy(
    user_id: str = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    policy = await media_policy_service.get_or_create_policy(db)
    await db.commit()
    audit_log("admin.media_policy_read", user_id=user_id, success=True)
    return _policy_to_dict(policy)


@admin_router.patch("")
async def update_policy(
    update: PolicyUpdate,
    user_id: str = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    kwargs: dict[str, Any] = {}
    if update.global_max_width is not None:
        kwargs["global_max_width"] = update.global_max_width
    if update.global_max_height is not None:
        kwargs["global_max_height"] = update.global_max_height
    if update.global_max_framerate is not None:
        kwargs["global_max_framerate"] = update.global_max_framerate
    if update.global_max_bitrate_kbps is not None:
        kwargs["global_max_bitrate_kbps"] = update.global_max_bitrate_kbps
    if update.allow_8k is not None:
        kwargs["allow_8k"] = update.allow_8k
    if update.allow_client_override is not None:
        kwargs["allow_client_override"] = update.allow_client_override
    if update.enforce_hard_cap is not None:
        kwargs["enforce_hard_cap"] = update.enforce_hard_cap
    if update.transcoding_enabled is not None:
        kwargs["transcoding_enabled"] = update.transcoding_enabled
    if update.prefer_hw_encoder is not None:
        kwargs["prefer_hw_encoder"] = update.prefer_hw_encoder
    if update.auto_max_quality is not None:
        kwargs["auto_max_quality"] = update.auto_max_quality
    if update.role_caps is not None:
        kwargs["role_caps"] = {k: v.model_dump() for k, v in update.role_caps.items()}

    try:
        policy = await media_policy_service.update_policy(db, **kwargs)
        await db.commit()
    except Exception as e:
        await db.rollback()
        logger.error("media_policy_update_failed", error=str(e), user_id=user_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update media policy",
        )

    audit_log("admin.media_policy_update", user_id=user_id, success=True, details=kwargs)
    return _policy_to_dict(policy)


@admin_router.get("/overrides")
async def list_overrides(
    user_id: str = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    rows = await media_policy_service.list_user_overrides(db)
    return {
        "overrides": [
            {
                "id": r.id,
                "user_id": r.user_id,
                "max_width": r.max_width,
                "max_height": r.max_height,
                "max_framerate": r.max_framerate,
                "max_bitrate_kbps": r.max_bitrate_kbps,
                "note": r.note,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            }
            for r in rows
        ],
    }


@admin_router.put("/overrides/{target_user_id}")
async def upsert_override(
    target_user_id: str,
    payload: UserOverrideUpsert,
    user_id: str = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    try:
        override = await media_policy_service.set_user_override(
            db,
            target_user_id,
            max_width=payload.max_width,
            max_height=payload.max_height,
            max_framerate=payload.max_framerate,
            max_bitrate_kbps=payload.max_bitrate_kbps,
            note=payload.note,
        )
        await db.commit()
    except Exception as e:
        await db.rollback()
        logger.error("media_override_upsert_failed", error=str(e), user_id=user_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to set override",
        )

    audit_log(
        "admin.media_override_set",
        user_id=user_id,
        success=True,
        details={"target_user_id": target_user_id, **payload.model_dump(exclude_none=True)},
    )
    return {
        "id": override.id,
        "user_id": override.user_id,
        "max_width": override.max_width,
        "max_height": override.max_height,
        "max_framerate": override.max_framerate,
        "max_bitrate_kbps": override.max_bitrate_kbps,
        "note": override.note,
    }


@admin_router.delete("/overrides/{target_user_id}")
async def clear_override(
    target_user_id: str,
    user_id: str = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    removed = await media_policy_service.clear_user_override(db, target_user_id)
    await db.commit()
    audit_log(
        "admin.media_override_clear",
        user_id=user_id,
        success=removed,
        details={"target_user_id": target_user_id},
    )
    if not removed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No override for user {target_user_id}",
        )
    return {"status": "cleared", "user_id": target_user_id}


# ── Admin: camera quality presets ─────────────────────────

@admin_router.get("/presets")
async def admin_list_presets(
    user_id: str = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """List every preset, including disabled rows. Unlike the user list
    this does NOT clamp against any particular user's cap — admins see
    the raw numbers so they can edit ceilings."""
    presets = await media_policy_service.list_presets(db, include_disabled=True)
    return {
        "presets": [media_policy_service.preset_to_dict(p) for p in presets],
    }


@admin_router.post("/presets", status_code=status.HTTP_201_CREATED)
async def admin_create_preset(
    payload: PresetCreate,
    user_id: str = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    try:
        preset = await media_policy_service.create_preset(
            db,
            id=payload.id,
            label=payload.label,
            description=payload.description,
            width=payload.width,
            height=payload.height,
            framerate=payload.framerate,
            bitrate_kbps=payload.bitrate_kbps,
            codec_preference=payload.codec_preference,
            requires_8k=payload.requires_8k,
            enabled=payload.enabled,
            is_default=payload.is_default,
            sort_order=payload.sort_order,
        )
        await db.commit()
    except ValueError as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(e),
        )
    except Exception as e:
        await db.rollback()
        logger.error("preset_create_failed", user_id=user_id, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create preset",
        )
    audit_log(
        "admin.camera_preset_create",
        user_id=user_id, success=True,
        details={"preset_id": preset.id, "label": preset.label},
    )
    return media_policy_service.preset_to_dict(preset)


@admin_router.patch("/presets/{preset_id}")
async def admin_update_preset(
    preset_id: str,
    payload: PresetPatch,
    user_id: str = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    try:
        preset = await media_policy_service.update_preset(
            db, preset_id, **payload.model_dump(exclude_none=True),
        )
        if preset is None:
            await db.rollback()
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No preset {preset_id}",
            )
        await db.commit()
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error("preset_update_failed", user_id=user_id, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update preset",
        )
    audit_log(
        "admin.camera_preset_update",
        user_id=user_id, success=True,
        details={"preset_id": preset_id, **payload.model_dump(exclude_none=True)},
    )
    return media_policy_service.preset_to_dict(preset)


@admin_router.delete("/presets/{preset_id}")
async def admin_delete_preset(
    preset_id: str,
    user_id: str = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    deleted, reason = await media_policy_service.delete_preset(db, preset_id)
    if not deleted:
        await db.rollback()
        if reason == "not_found":
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No preset {preset_id}",
            )
        if reason == "builtin_cannot_delete":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Builtin presets can be disabled but not deleted",
            )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete preset ({reason})",
        )
    await db.commit()
    audit_log(
        "admin.camera_preset_delete",
        user_id=user_id, success=True,
        details={"preset_id": preset_id},
    )
    return {"status": "deleted", "preset_id": preset_id}
