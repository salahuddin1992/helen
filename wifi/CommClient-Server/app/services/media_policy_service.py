"""
Media policy service — resolves the effective max resolution / framerate /
bitrate for a given user, and exposes admin-facing mutations.

Resolution order (most → least specific):
  1. UserMediaOverride row (if present)
  2. role_caps_json[user.role]
  3. global_* columns on MediaPolicy
Clamping is always at most the global value — role and per-user caps can
never raise the ceiling above the global policy (defence in depth: admins
only loosen things by raising the global cap explicitly).

Well-known resolution ladder (used by the API to validate client requests
and by the UI to render a dropdown):

  240p   = 426×240    @ 15fps, 400kbps
  360p   = 640×360    @ 24fps, 800kbps
  480p   = 854×480    @ 24fps, 1500kbps
  720p   = 1280×720   @ 30fps, 3000kbps
  1080p  = 1920×1080  @ 30fps, 6000kbps
  1440p  = 2560×1440  @ 30fps, 12000kbps
  2160p  = 3840×2160  @ 30fps, 25000kbps  (4K)
  2160p60= 3840×2160  @ 60fps, 40000kbps  (4K60)
  4320p  = 7680×4320  @ 30fps, 60000kbps  (8K) — gated by allow_8k
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.media_policy import (
    CameraQualityPreset,
    MediaPolicy,
    UserMediaOverride,
)
from app.models.user import User

logger = get_logger(__name__)


GLOBAL_ID = "global"


# Builtin camera presets — seeded on first startup and re-asserted every
# boot. Each row is identified by a stable string id so the client can
# ship a matching icon/order and so admin edits persist across reseeds
# (the seeder only touches MISSING rows; existing ones are left alone).
BUILTIN_CAMERA_PRESETS: list[dict[str, Any]] = [
    # id, label, description, w, h, fps, kbps, requires_8k, is_default, sort_order
    {"id": "audio-only",  "label": "Audio only",      "description": "No camera — voice only",                           "width":    0, "height":    0, "framerate":  0, "bitrate_kbps":     0, "requires_8k": False, "is_default": False, "sort_order":  10},
    {"id": "data-saver",  "label": "Data saver",      "description": "Lowest camera quality, for slow / metered links",  "width":  426, "height":  240, "framerate": 15, "bitrate_kbps":   400, "requires_8k": False, "is_default": False, "sort_order":  20},
    {"id": "360p",        "label": "360p",            "description": "Light bandwidth, small windows",                   "width":  640, "height":  360, "framerate": 24, "bitrate_kbps":   800, "requires_8k": False, "is_default": False, "sort_order":  30},
    {"id": "480p",        "label": "480p SD",         "description": "Standard-definition video",                        "width":  854, "height":  480, "framerate": 24, "bitrate_kbps":  1500, "requires_8k": False, "is_default": False, "sort_order":  40},
    {"id": "720p",        "label": "720p HD",         "description": "HD video — good default for most calls",           "width": 1280, "height":  720, "framerate": 30, "bitrate_kbps":  3000, "requires_8k": False, "is_default": True,  "sort_order":  50},
    {"id": "1080p",       "label": "1080p Full HD",   "description": "Full-HD video, higher bandwidth",                  "width": 1920, "height": 1080, "framerate": 30, "bitrate_kbps":  6000, "requires_8k": False, "is_default": False, "sort_order":  60},
    {"id": "1440p",       "label": "1440p QHD",       "description": "Quad-HD video for large displays",                 "width": 2560, "height": 1440, "framerate": 30, "bitrate_kbps": 12000, "requires_8k": False, "is_default": False, "sort_order":  70},
    {"id": "4k",          "label": "4K (2160p)",      "description": "Ultra-HD video at 30fps",                          "width": 3840, "height": 2160, "framerate": 30, "bitrate_kbps": 25000, "requires_8k": False, "is_default": False, "sort_order":  80},
    {"id": "4k60",        "label": "4K60 (2160p60)",  "description": "Ultra-HD at 60fps — heavy bandwidth",              "width": 3840, "height": 2160, "framerate": 60, "bitrate_kbps": 40000, "requires_8k": False, "is_default": False, "sort_order":  90},
    {"id": "8k",          "label": "8K (4320p)",      "description": "8K video — requires allow_8k and a capable link",  "width": 7680, "height": 4320, "framerate": 30, "bitrate_kbps": 60000, "requires_8k": True,  "is_default": False, "sort_order": 100},
    {"id": "8k60",        "label": "8K60",            "description": "8K at 60fps — highest built-in preset",            "width": 7680, "height": 4320, "framerate": 60, "bitrate_kbps": 80000, "requires_8k": True,  "is_default": False, "sort_order": 110},
    {"id": "higher",      "label": "Higher (custom)", "description": "Placeholder for admin-defined >8K setups",         "width": 7680, "height": 4320, "framerate": 60, "bitrate_kbps": 80000, "requires_8k": True,  "is_default": False, "sort_order": 120},
]


@dataclass(frozen=True)
class EffectiveCap:
    max_width: int
    max_height: int
    max_framerate: int
    max_bitrate_kbps: int
    allow_8k: bool
    allow_client_override: bool
    enforce_hard_cap: bool
    auto_max_quality: bool
    source: str  # "user_override" | "role" | "global"

    def as_dict(self) -> dict[str, Any]:
        return {
            "max_width": self.max_width,
            "max_height": self.max_height,
            "max_framerate": self.max_framerate,
            "max_bitrate_kbps": self.max_bitrate_kbps,
            "allow_8k": self.allow_8k,
            "allow_client_override": self.allow_client_override,
            "enforce_hard_cap": self.enforce_hard_cap,
            "auto_max_quality": self.auto_max_quality,
            "source": self.source,
        }


# Canonical ladder — ordered low → high so the UI can render a dropdown
# with a consistent sort. `kbps` is a sensible default bitrate for a
# single primary stream; simulcast layers scale down from there.
RESOLUTION_LADDER: list[dict[str, Any]] = [
    {"id": "audio-only", "label": "Audio only",    "w":    0, "h":    0, "fps":  0, "kbps":     0},
    {"id": "240p",       "label": "240p",          "w":  426, "h":  240, "fps": 15, "kbps":   400},
    {"id": "360p",       "label": "360p",          "w":  640, "h":  360, "fps": 24, "kbps":   800},
    {"id": "480p",       "label": "480p",          "w":  854, "h":  480, "fps": 24, "kbps":  1500},
    {"id": "720p",       "label": "720p HD",       "w": 1280, "h":  720, "fps": 30, "kbps":  3000},
    {"id": "1080p",      "label": "1080p Full HD", "w": 1920, "h": 1080, "fps": 30, "kbps":  6000},
    {"id": "1440p",      "label": "1440p QHD",     "w": 2560, "h": 1440, "fps": 30, "kbps": 12000},
    {"id": "2160p",      "label": "2160p (4K)",    "w": 3840, "h": 2160, "fps": 30, "kbps": 25000},
    {"id": "2160p60",    "label": "2160p60 (4K60)","w": 3840, "h": 2160, "fps": 60, "kbps": 40000},
    {"id": "4320p",      "label": "4320p (8K)",    "w": 7680, "h": 4320, "fps": 30, "kbps": 60000, "requires_8k": True},
]


def get_resolution_ladder(allow_8k: bool) -> list[dict[str, Any]]:
    """Return the ladder with 8K entries stripped if the policy forbids them."""
    return [r for r in RESOLUTION_LADDER if allow_8k or not r.get("requires_8k")]


class MediaPolicyService:
    """Async service backed by the `media_policies` / `user_media_overrides` tables."""

    # ── Policy CRUD ─────────────────────────────────────────

    async def get_or_create_policy(self, db: AsyncSession) -> MediaPolicy:
        """Return the singleton policy row, creating it with defaults if missing."""
        result = await db.execute(
            select(MediaPolicy).where(MediaPolicy.id == GLOBAL_ID)
        )
        policy = result.scalar_one_or_none()
        if policy is None:
            policy = MediaPolicy(id=GLOBAL_ID)
            db.add(policy)
            await db.flush()
            logger.info("media_policy_created_default")
        return policy

    async def update_policy(
        self,
        db: AsyncSession,
        *,
        global_max_width: int | None = None,
        global_max_height: int | None = None,
        global_max_framerate: int | None = None,
        global_max_bitrate_kbps: int | None = None,
        allow_8k: bool | None = None,
        allow_client_override: bool | None = None,
        enforce_hard_cap: bool | None = None,
        role_caps: dict[str, dict[str, int]] | None = None,
        transcoding_enabled: bool | None = None,
        prefer_hw_encoder: bool | None = None,
        auto_max_quality: bool | None = None,
    ) -> MediaPolicy:
        policy = await self.get_or_create_policy(db)
        if global_max_width is not None:
            policy.global_max_width = max(160, min(7680, int(global_max_width)))
        if global_max_height is not None:
            policy.global_max_height = max(120, min(4320, int(global_max_height)))
        if global_max_framerate is not None:
            policy.global_max_framerate = max(1, min(120, int(global_max_framerate)))
        if global_max_bitrate_kbps is not None:
            policy.global_max_bitrate_kbps = max(0, min(200_000, int(global_max_bitrate_kbps)))
        if allow_8k is not None:
            policy.allow_8k = bool(allow_8k)
        if allow_client_override is not None:
            policy.allow_client_override = bool(allow_client_override)
        if enforce_hard_cap is not None:
            policy.enforce_hard_cap = bool(enforce_hard_cap)
        if role_caps is not None:
            policy.role_caps_json = json.dumps(role_caps)
        if transcoding_enabled is not None:
            policy.transcoding_enabled = bool(transcoding_enabled)
        if prefer_hw_encoder is not None:
            policy.prefer_hw_encoder = bool(prefer_hw_encoder)
        if auto_max_quality is not None:
            policy.auto_max_quality = bool(auto_max_quality)
        await db.flush()
        return policy

    # ── Per-user overrides ──────────────────────────────────

    async def set_user_override(
        self,
        db: AsyncSession,
        user_id: str,
        *,
        max_width: int | None = None,
        max_height: int | None = None,
        max_framerate: int | None = None,
        max_bitrate_kbps: int | None = None,
        note: str | None = None,
    ) -> UserMediaOverride:
        result = await db.execute(
            select(UserMediaOverride).where(UserMediaOverride.user_id == user_id)
        )
        override = result.scalar_one_or_none()
        if override is None:
            override = UserMediaOverride(user_id=user_id)
            db.add(override)
        override.max_width = max_width
        override.max_height = max_height
        override.max_framerate = max_framerate
        override.max_bitrate_kbps = max_bitrate_kbps
        override.note = note
        await db.flush()
        return override

    async def clear_user_override(self, db: AsyncSession, user_id: str) -> bool:
        result = await db.execute(
            select(UserMediaOverride).where(UserMediaOverride.user_id == user_id)
        )
        override = result.scalar_one_or_none()
        if override is None:
            return False
        await db.delete(override)
        await db.flush()
        return True

    async def list_user_overrides(
        self, db: AsyncSession,
    ) -> list[UserMediaOverride]:
        result = await db.execute(select(UserMediaOverride))
        return list(result.scalars().all())

    # ── Resolution ──────────────────────────────────────────

    async def effective_cap_for(
        self, db: AsyncSession, user_id: str,
    ) -> EffectiveCap:
        """Resolve the final cap for one user, falling back role → global."""
        policy = await self.get_or_create_policy(db)

        # Look up the user's role (may be None if user was deleted).
        user_result = await db.execute(select(User).where(User.id == user_id))
        user = user_result.scalar_one_or_none()
        role = user.role if user else "user"

        # Start from the global ceiling and narrow inward.
        cap_w = policy.global_max_width
        cap_h = policy.global_max_height
        cap_fps = policy.global_max_framerate
        cap_kbps = policy.global_max_bitrate_kbps
        source = "global"

        # Role layer.
        try:
            role_caps = json.loads(policy.role_caps_json or "{}")
        except (ValueError, TypeError):
            role_caps = {}
        rc = role_caps.get(role) if isinstance(role_caps, dict) else None
        if isinstance(rc, dict):
            cap_w = min(cap_w, int(rc.get("max_w", cap_w)))
            cap_h = min(cap_h, int(rc.get("max_h", cap_h)))
            cap_fps = min(cap_fps, int(rc.get("max_fps", cap_fps)))
            cap_kbps = min(cap_kbps, int(rc.get("max_kbps", cap_kbps)))
            source = "role"

        # User override — allowed to narrow further, never widen past global.
        override_result = await db.execute(
            select(UserMediaOverride).where(UserMediaOverride.user_id == user_id)
        )
        override = override_result.scalar_one_or_none()
        if override is not None:
            if override.max_width is not None:
                cap_w = min(cap_w, int(override.max_width))
            if override.max_height is not None:
                cap_h = min(cap_h, int(override.max_height))
            if override.max_framerate is not None:
                cap_fps = min(cap_fps, int(override.max_framerate))
            if override.max_bitrate_kbps is not None:
                cap_kbps = min(cap_kbps, int(override.max_bitrate_kbps))
            source = "user_override"

        # 8K gate: if not allowed globally, clamp the effective cap below 8K.
        if not policy.allow_8k:
            cap_w = min(cap_w, 3840)
            cap_h = min(cap_h, 2160)

        return EffectiveCap(
            max_width=cap_w,
            max_height=cap_h,
            max_framerate=cap_fps,
            max_bitrate_kbps=cap_kbps,
            allow_8k=policy.allow_8k,
            allow_client_override=policy.allow_client_override,
            enforce_hard_cap=policy.enforce_hard_cap,
            auto_max_quality=policy.auto_max_quality,
            source=source,
        )

    # ── Camera quality presets ─────────────────────────────

    async def seed_builtin_presets(self, db: AsyncSession) -> int:
        """Idempotently insert any missing builtin preset rows.

        Only creates rows whose `id` doesn't already exist — admins can
        disable/edit builtins after seed and those edits persist across
        restarts (the seeder never overwrites existing rows).

        Returns the count of rows inserted this call.
        """
        result = await db.execute(select(CameraQualityPreset.id))
        existing = {row[0] for row in result.all()}
        inserted = 0
        for spec in BUILTIN_CAMERA_PRESETS:
            if spec["id"] in existing:
                continue
            db.add(CameraQualityPreset(
                id=spec["id"],
                label=spec["label"],
                description=spec.get("description"),
                width=spec["width"],
                height=spec["height"],
                framerate=spec["framerate"],
                bitrate_kbps=spec["bitrate_kbps"],
                codec_preference="auto",
                requires_8k=spec.get("requires_8k", False),
                is_builtin=True,
                is_default=spec.get("is_default", False),
                enabled=True,
                sort_order=spec.get("sort_order", 100),
            ))
            inserted += 1
        if inserted:
            await db.flush()
            logger.info("camera_presets_seeded", count=inserted)
        return inserted

    async def list_presets(
        self,
        db: AsyncSession,
        *,
        include_disabled: bool = False,
    ) -> list[CameraQualityPreset]:
        stmt = select(CameraQualityPreset)
        if not include_disabled:
            stmt = stmt.where(CameraQualityPreset.enabled.is_(True))
        stmt = stmt.order_by(
            CameraQualityPreset.sort_order.asc(),
            CameraQualityPreset.label.asc(),
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def get_preset(
        self, db: AsyncSession, preset_id: str,
    ) -> CameraQualityPreset | None:
        result = await db.execute(
            select(CameraQualityPreset).where(CameraQualityPreset.id == preset_id)
        )
        return result.scalar_one_or_none()

    async def get_default_preset(
        self, db: AsyncSession,
    ) -> CameraQualityPreset | None:
        """Return the one preset flagged is_default=True, if any.

        Falls back to the first enabled preset by sort_order if no row is
        flagged (e.g. admin deleted the default without choosing another).
        """
        result = await db.execute(
            select(CameraQualityPreset)
            .where(
                CameraQualityPreset.is_default.is_(True),
                CameraQualityPreset.enabled.is_(True),
            )
            .limit(1)
        )
        preset = result.scalar_one_or_none()
        if preset is not None:
            return preset
        result = await db.execute(
            select(CameraQualityPreset)
            .where(CameraQualityPreset.enabled.is_(True))
            .order_by(CameraQualityPreset.sort_order.asc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def create_preset(
        self,
        db: AsyncSession,
        *,
        id: str,
        label: str,
        width: int,
        height: int,
        framerate: int,
        bitrate_kbps: int,
        description: str | None = None,
        codec_preference: str = "auto",
        requires_8k: bool = False,
        enabled: bool = True,
        is_default: bool = False,
        sort_order: int = 100,
    ) -> CameraQualityPreset:
        if await self.get_preset(db, id) is not None:
            raise ValueError(f"preset id already exists: {id}")
        preset = CameraQualityPreset(
            id=id,
            label=label,
            description=description,
            width=max(0, min(16_000, int(width))),
            height=max(0, min(16_000, int(height))),
            framerate=max(0, min(240, int(framerate))),
            bitrate_kbps=max(0, min(1_000_000, int(bitrate_kbps))),
            codec_preference=codec_preference,
            requires_8k=bool(requires_8k),
            is_builtin=False,
            is_default=bool(is_default),
            enabled=bool(enabled),
            sort_order=int(sort_order),
        )
        db.add(preset)
        if is_default:
            await self._clear_other_defaults(db, keep_id=id)
        await db.flush()
        return preset

    async def update_preset(
        self,
        db: AsyncSession,
        preset_id: str,
        **fields: Any,
    ) -> CameraQualityPreset | None:
        preset = await self.get_preset(db, preset_id)
        if preset is None:
            return None
        # Allowed fields to mutate. Note: `id` and `is_builtin` are intentionally
        # NOT here — they're structural.
        allowed = {
            "label", "description", "width", "height", "framerate",
            "bitrate_kbps", "codec_preference", "requires_8k", "enabled",
            "is_default", "sort_order",
        }
        for key, value in fields.items():
            if value is None or key not in allowed:
                continue
            if key in {"width", "height"}:
                value = max(0, min(16_000, int(value)))
            elif key == "framerate":
                value = max(0, min(240, int(value)))
            elif key == "bitrate_kbps":
                value = max(0, min(1_000_000, int(value)))
            elif key in {"requires_8k", "enabled", "is_default"}:
                value = bool(value)
            elif key == "sort_order":
                value = int(value)
            setattr(preset, key, value)
        if fields.get("is_default") is True:
            await self._clear_other_defaults(db, keep_id=preset.id)
        await db.flush()
        return preset

    async def delete_preset(
        self, db: AsyncSession, preset_id: str,
    ) -> tuple[bool, str]:
        """Delete a non-builtin preset. Returns (deleted, reason)."""
        preset = await self.get_preset(db, preset_id)
        if preset is None:
            return False, "not_found"
        if preset.is_builtin:
            # Builtins are disable-only so the client's stable id list survives.
            return False, "builtin_cannot_delete"
        await db.delete(preset)
        await db.flush()
        return True, "ok"

    async def _clear_other_defaults(
        self, db: AsyncSession, *, keep_id: str,
    ) -> None:
        await db.execute(
            update(CameraQualityPreset)
            .where(
                CameraQualityPreset.id != keep_id,
                CameraQualityPreset.is_default.is_(True),
            )
            .values(is_default=False)
        )

    async def set_user_active_preset(
        self, db: AsyncSession, user_id: str, preset_id: str | None,
    ) -> CameraQualityPreset | None:
        """Point the user at a preset, or clear with preset_id=None.

        Returns the resolved preset row the user will now use (after
        fallback to the server default when cleared).
        """
        user_result = await db.execute(select(User).where(User.id == user_id))
        user = user_result.scalar_one_or_none()
        if user is None:
            raise ValueError(f"unknown user: {user_id}")

        if preset_id is not None:
            preset = await self.get_preset(db, preset_id)
            if preset is None:
                raise ValueError(f"unknown preset: {preset_id}")
            if not preset.enabled:
                raise ValueError(f"preset disabled: {preset_id}")
            user.active_camera_preset_id = preset_id
        else:
            user.active_camera_preset_id = None

        await db.flush()
        return await self.resolve_active_preset(db, user_id)

    async def resolve_active_preset(
        self, db: AsyncSession, user_id: str,
    ) -> CameraQualityPreset | None:
        """Return the preset the user is currently using — their pick if
        still enabled, otherwise the server default."""
        user_result = await db.execute(select(User).where(User.id == user_id))
        user = user_result.scalar_one_or_none()
        if user is None:
            return None
        if user.active_camera_preset_id:
            preset = await self.get_preset(db, user.active_camera_preset_id)
            if preset is not None and preset.enabled:
                return preset
        return await self.get_default_preset(db)

    def preset_to_dict(
        self,
        preset: CameraQualityPreset,
        *,
        cap: EffectiveCap | None = None,
    ) -> dict[str, Any]:
        """Serialize a preset for API output.

        When `cap` is provided we also emit the clamped encoder values
        the user is actually allowed to use — the client should target
        these, not the raw preset values. Useful for `/media-policy/me`
        so the dropdown shows effective numbers, not the preset's ideal.
        """
        out: dict[str, Any] = {
            "id": preset.id,
            "label": preset.label,
            "description": preset.description,
            "width": preset.width,
            "height": preset.height,
            "framerate": preset.framerate,
            "bitrate_kbps": preset.bitrate_kbps,
            "codec_preference": preset.codec_preference,
            "requires_8k": preset.requires_8k,
            "is_builtin": preset.is_builtin,
            "is_default": preset.is_default,
            "enabled": preset.enabled,
            "sort_order": preset.sort_order,
        }
        if cap is not None:
            effective_w = min(preset.width, cap.max_width)
            effective_h = min(preset.height, cap.max_height)
            effective_fps = min(preset.framerate, cap.max_framerate) if preset.framerate else 0
            effective_kbps = min(preset.bitrate_kbps, cap.max_bitrate_kbps)
            out["effective"] = {
                "width": effective_w,
                "height": effective_h,
                "framerate": effective_fps,
                "bitrate_kbps": effective_kbps,
                "clamped": (
                    effective_w != preset.width
                    or effective_h != preset.height
                    or effective_fps != preset.framerate
                    or effective_kbps != preset.bitrate_kbps
                ),
            }
            # Hide 8K-gated presets when the policy forbids 8K so the UI
            # doesn't dangle a preset the user can't actually use.
            out["available"] = (
                preset.enabled
                and (not preset.requires_8k or cap.allow_8k)
            )
        return out

    async def clamp_request(
        self,
        db: AsyncSession,
        user_id: str,
        *,
        width: int,
        height: int,
        framerate: int,
        bitrate_kbps: int,
    ) -> tuple[int, int, int, int, bool]:
        """
        Clamp a client-requested set of encoding params to the effective cap.
        Returns (w, h, fps, kbps, was_clamped).
        """
        cap = await self.effective_cap_for(db, user_id)
        new_w = min(max(0, width), cap.max_width)
        new_h = min(max(0, height), cap.max_height)
        new_fps = min(max(0, framerate), cap.max_framerate)
        new_kbps = min(max(0, bitrate_kbps), cap.max_bitrate_kbps)
        clamped = (
            new_w != width
            or new_h != height
            or new_fps != framerate
            or new_kbps != bitrate_kbps
        )
        if clamped:
            logger.info(
                "media_cap_clamped",
                user_id=user_id,
                requested=f"{width}x{height}@{framerate}fps/{bitrate_kbps}kbps",
                granted=f"{new_w}x{new_h}@{new_fps}fps/{new_kbps}kbps",
                source=cap.source,
            )
        return new_w, new_h, new_fps, new_kbps, clamped


# Process-level singleton
media_policy_service = MediaPolicyService()

__all__ = [
    "media_policy_service",
    "MediaPolicyService",
    "EffectiveCap",
    "RESOLUTION_LADDER",
    "BUILTIN_CAMERA_PRESETS",
    "get_resolution_ladder",
]
