"""Screen-share quality presets + region selection.

Lets the client request a named preset instead of negotiating raw
mediasoup parameters. Presets:

  * low      — 720p × 15fps × 600 kbps  (text apps, low-bandwidth)
  * medium   — 1080p × 24fps × 2 Mbps  (default)
  * high     — 1080p × 30fps × 5 Mbps  (motion-heavy)
  * source   — native resolution × 30fps × 8 Mbps (presenter screen)

Region selection lets the presenter restrict the share to a
rectangle (privacy / performance). The server stores the preset +
region per session; the SFU honours it via simulcast layer choice.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class QualityPreset(str, Enum):
    MICRO  = "micro"   # very constrained links (cellular fallback)
    LOW    = "low"
    MEDIUM = "medium"
    HIGH   = "high"
    ULTRA  = "ultra"   # 4K presenter screens
    SOURCE = "source"


@dataclass(frozen=True)
class _Profile:
    width:    int
    height:   int
    fps:      int
    bitrate_kbps: int


_PROFILES: dict[QualityPreset, _Profile] = {
    QualityPreset.MICRO:  _Profile(854,  480,  10,   200),
    QualityPreset.LOW:    _Profile(1280, 720,  15,   600),
    QualityPreset.MEDIUM: _Profile(1920, 1080, 24, 2_000),
    QualityPreset.HIGH:   _Profile(1920, 1080, 30, 5_000),
    QualityPreset.ULTRA:  _Profile(3840, 2160, 30, 12_000),
    QualityPreset.SOURCE: _Profile(0,    0,    30, 8_000),
}


def auto_select_preset(available_kbps: int) -> QualityPreset:
    """Pick the best preset whose bitrate fits the available headroom.
    Used by the desktop client when probed bandwidth is known and the
    user has selected 'auto' instead of a fixed preset."""
    if available_kbps <= 0:
        return QualityPreset.MEDIUM
    if available_kbps < 400:
        return QualityPreset.MICRO
    if available_kbps < 1_200:
        return QualityPreset.LOW
    if available_kbps < 3_500:
        return QualityPreset.MEDIUM
    if available_kbps < 8_000:
        return QualityPreset.HIGH
    return QualityPreset.ULTRA


@dataclass
class Region:
    x: int = 0
    y: int = 0
    width: int = 0    # 0 = whole screen
    height: int = 0   # 0 = whole screen

    def is_full_screen(self) -> bool:
        return self.width == 0 and self.height == 0

    def to_dict(self) -> dict:
        return {
            "x": self.x, "y": self.y,
            "width": self.width, "height": self.height,
            "full_screen": self.is_full_screen(),
        }


def profile_for(preset: QualityPreset | str) -> dict:
    """Return media constraints for the chosen preset."""
    if isinstance(preset, str):
        try:
            preset = QualityPreset(preset.lower())
        except ValueError:
            preset = QualityPreset.MEDIUM
    p = _PROFILES[preset]
    return {
        "preset":       preset.value,
        "width":        p.width,
        "height":       p.height,
        "fps":          p.fps,
        "bitrate_kbps": p.bitrate_kbps,
    }


def all_presets() -> list[dict]:
    return [
        {"name": k.value, **profile_for(k)} for k in QualityPreset
    ]


def configure_session(
    room_id: str,
    preset: QualityPreset | str = QualityPreset.MEDIUM,
    region: Optional[Region] = None,
) -> dict:
    """Update the screen-share registry session with quality + region."""
    from app.services.screen_share_session import get_screen_share_registry
    sess = get_screen_share_registry().get(room_id)
    if sess is None:
        return {"ok": False, "error": "no_active_session"}

    profile = profile_for(preset)
    region_dict = (region or Region()).to_dict()
    # Stash on the session via setattr for read-back.
    setattr(sess, "_quality", profile)
    setattr(sess, "_region", region_dict)
    return {
        "ok":      True,
        "room_id": room_id,
        "profile": profile,
        "region":  region_dict,
    }


def get_session_quality(room_id: str) -> dict:
    from app.services.screen_share_session import get_screen_share_registry
    sess = get_screen_share_registry().get(room_id)
    if sess is None:
        return {"ok": False, "error": "no_active_session"}
    return {
        "ok":      True,
        "room_id": room_id,
        "profile": getattr(sess, "_quality", profile_for(QualityPreset.MEDIUM)),
        "region":  getattr(sess, "_region", Region().to_dict()),
    }
