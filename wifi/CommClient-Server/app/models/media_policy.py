"""
Media policy, ingest source, and per-user override models.

Introduced so the LAN admin can cap camera/video resolution + bitrate at a
global level, per-role, and per-user, and so external cameras (RTSP/RTMP/
SRT/HTTP/NDI) can be registered as ingest sources that the FFmpeg supervisor
pulls and re-publishes into the existing mediasoup router.

Tables:
  media_policies          — singleton row (id='global') holding the global
                            defaults + per-role JSON caps.
  user_media_overrides    — one row per user that needs a different cap.
  ingest_sources          — one row per registered external camera feed.
  camera_quality_presets  — named camera preset (4K, 8K, 1080p, Lower, Higher…)
                            that users quick-pick and admins CRUD.
"""

from __future__ import annotations

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class MediaPolicy(Base, TimestampMixin):
    """
    Global + per-role media policy. Exactly one row is expected with
    id='global'; the service layer upserts it on first access.
    """
    __tablename__ = "media_policies"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default="global")

    # Global caps applied to every user (admin can still override per-role).
    global_max_width: Mapped[int] = mapped_column(Integer, nullable=False, default=7680)
    global_max_height: Mapped[int] = mapped_column(Integer, nullable=False, default=4320)
    global_max_framerate: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    global_max_bitrate_kbps: Mapped[int] = mapped_column(Integer, nullable=False, default=80_000)

    # Feature gates.
    allow_8k: Mapped[bool] = mapped_column(default=True, nullable=False)
    allow_client_override: Mapped[bool] = mapped_column(default=True, nullable=False)
    enforce_hard_cap: Mapped[bool] = mapped_column(default=True, nullable=False)

    # JSON-serialised per-role overrides: {"admin": {"max_w":7680,...}, ...}.
    # Stored as TEXT for SQLite-portability.
    role_caps_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")

    # Transcoding toggles.
    transcoding_enabled: Mapped[bool] = mapped_column(default=True, nullable=False)
    prefer_hw_encoder: Mapped[bool] = mapped_column(default=True, nullable=False)

    # When True, the client is advised to probe each camera / mic for its
    # real maximum capability (getCapabilities) and capture at that max,
    # still clamped server-side by the enforce_hard_cap ceiling. Off by
    # default so existing deployments keep the current preset flow.
    # server_default is required so _align_model_columns can add this to
    # pre-existing DBs without violating NOT NULL.
    auto_max_quality: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0"),
    )

    def __repr__(self) -> str:
        return f"<MediaPolicy {self.id} {self.global_max_width}x{self.global_max_height}@{self.global_max_framerate}>"


class UserMediaOverride(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Per-user cap override. Absent → role defaults apply."""
    __tablename__ = "user_media_overrides"

    user_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    max_width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_framerate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_bitrate_kbps: Mapped[int | None] = mapped_column(Integer, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return f"<UserMediaOverride user={self.user_id[:8]}>"


class IngestSource(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """
    Registered external camera feed. FFmpeg pulls from `url`, transcodes
    (optionally via NVENC/QSV), and re-publishes into the SFU as a producer
    owned by `owner_user_id`.
    """
    __tablename__ = "ingest_sources"

    owner_user_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    protocol: Mapped[str] = mapped_column(String(16), nullable=False)  # rtsp|rtmp|srt|http|hls|file|ndi
    url: Mapped[str] = mapped_column(Text, nullable=False)
    username: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # NB: password stored in clear in the LAN DB. Rotate per-camera or move
    # to persistent_secrets if the threat model tightens.
    password: Mapped[str | None] = mapped_column(String(256), nullable=True)

    transport: Mapped[str] = mapped_column(String(8), nullable=False, default="tcp")  # tcp|udp
    codec_hint: Mapped[str | None] = mapped_column(String(16), nullable=True)  # h264|hevc|mjpeg|av1
    target_width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target_height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target_framerate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target_bitrate_kbps: Mapped[int | None] = mapped_column(Integer, nullable=True)

    enabled: Mapped[bool] = mapped_column(default=True, nullable=False)
    auto_start: Mapped[bool] = mapped_column(default=False, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="idle",
    )  # idle|starting|running|error|stopped
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return f"<IngestSource {self.name} {self.protocol} ({self.status})>"


class CameraQualityPreset(Base, TimestampMixin):
    """
    Named camera capture/encode preset that appears in the client's
    quick-pick dropdown. Standard rungs (240p → 8K) are seeded as
    `is_builtin=True` at startup and can be disabled but not deleted.
    Admins can add custom presets (e.g. "Studio 12K", "Data-saver 180p")
    freely. At call-setup the preset is always clamped down to the user's
    effective cap from MediaPolicy, so a preset can never raise a cap
    above what the admin allows.
    """
    __tablename__ = "camera_quality_presets"

    # Stable string id so seeded rows survive reinstall and the client can
    # ship hard-coded icons keyed off it. Custom user-created presets get
    # auto-generated UUID-ish ids at the service layer.
    id: Mapped[str] = mapped_column(String(64), primary_key=True)

    # Human label + longer description, both user-facing.
    label: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # Encoder targets. width=0/height=0 denotes audio-only.
    width: Mapped[int] = mapped_column(Integer, nullable=False, default=1280)
    height: Mapped[int] = mapped_column(Integer, nullable=False, default=720)
    framerate: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    bitrate_kbps: Mapped[int] = mapped_column(Integer, nullable=False, default=3000)
    # Hint to the mediasoup/ffmpeg layer — "auto" means "whatever the
    # negotiated RTP caps allow, tiebreak by prefer_hw_encoder".
    codec_preference: Mapped[str] = mapped_column(
        String(16), nullable=False, default="auto",
    )  # auto|h264|hevc|av1|vp8|vp9

    # Flags.
    requires_8k: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_builtin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # Low number = shown first. Lets the UI preserve a sensible ladder
    # even when admins add custom presets between the builtins.
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=100)

    def __repr__(self) -> str:
        return (
            f"<CameraQualityPreset {self.id} "
            f"{self.width}x{self.height}@{self.framerate}/{self.bitrate_kbps}kbps>"
        )
