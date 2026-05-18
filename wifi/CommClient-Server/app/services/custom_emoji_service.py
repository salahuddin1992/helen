"""
Custom emoji service — admin-uploadable PNG/SVG/WebP shortcodes.

Each emoji is stored as ``$DATA_DIR/custom_emoji/<id>.<ext>`` plus
a metadata row in ``$DATA_DIR/custom_emoji.json``. Lookup is keyed
by shortcode (e.g. ``:helen-wave:``); the client picker exposes
the same shortcodes alongside system Unicode emoji.

Why not a DB table
------------------
Same pattern as ``channel_slow_mode`` / ``channel_message_ttl``:
the metadata is a sidecar JSON. Files are blobs on disk under
``$DATA_DIR``. Avoiding a schema migration keeps this feature
opt-in for operators who don't need it.

Shortcodes
----------
A shortcode is ``[a-z0-9][a-z0-9_-]{1,30}`` (case-insensitive,
normalized to lowercase). The server enforces uniqueness.

Limits
------
* Max file size: 256 KiB (configurable via env).
* Allowed mimes: ``image/png``, ``image/webp``, ``image/svg+xml``,
  ``image/gif``.
* Max upload count per server: 1000 (sanity guard).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


_SHORTCODE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,30}$")
_ALLOWED_MIMES = {
    "image/png", "image/webp", "image/svg+xml", "image/gif",
}
_EXT_FOR_MIME = {
    "image/png": "png",
    "image/webp": "webp",
    "image/svg+xml": "svg",
    "image/gif": "gif",
}


@dataclass
class CustomEmoji:
    id: str
    shortcode: str
    mime: str
    size_bytes: int
    uploaded_by: str
    uploaded_at: float
    description: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "shortcode": self.shortcode,
            "mime": self.mime,
            "size_bytes": self.size_bytes,
            "uploaded_by": self.uploaded_by,
            "uploaded_at": self.uploaded_at,
            "description": self.description,
            "url": f"/api/custom-emoji/{self.id}/raw",
        }


class CustomEmojiError(Exception):
    pass


class _CustomEmojiStore:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.assets_dir = data_dir / "custom_emoji"
        self.metadata_path = data_dir / "custom_emoji.json"
        self._items: dict[str, CustomEmoji] = {}
        self._lock = threading.Lock()
        self._loaded = False

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self.metadata_path.is_file():
            return
        try:
            data = json.loads(self.metadata_path.read_text("utf-8"))
            for row in (data or []):
                e = CustomEmoji(
                    id=row["id"],
                    shortcode=row["shortcode"],
                    mime=row["mime"],
                    size_bytes=int(row.get("size_bytes", 0)),
                    uploaded_by=row.get("uploaded_by", ""),
                    uploaded_at=float(row.get("uploaded_at", 0)),
                    description=row.get("description", ""),
                )
                self._items[e.id] = e
        except Exception:
            # Stale / malformed metadata — start fresh rather than
            # crashing the whole feature. The on-disk blobs remain
            # but won't be served.
            self._items = {}

    def _save(self) -> None:
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            payload = [e.to_dict() for e in self._items.values()]
            tmp = self.metadata_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2), "utf-8")
            tmp.replace(self.metadata_path)
        except OSError:
            # Best-effort; the in-memory state is still authoritative
            # until the next restart.
            pass

    # ── Read API ─────────────────────────────────────────────

    def list_all(self) -> list[CustomEmoji]:
        with self._lock:
            self._load()
            return sorted(
                self._items.values(),
                key=lambda e: e.shortcode,
            )

    def get(self, emoji_id: str) -> Optional[CustomEmoji]:
        with self._lock:
            self._load()
            return self._items.get(emoji_id)

    def get_path(self, emoji_id: str) -> Optional[Path]:
        e = self.get(emoji_id)
        if not e:
            return None
        ext = _EXT_FOR_MIME.get(e.mime, "bin")
        path = self.assets_dir / f"{emoji_id}.{ext}"
        return path if path.is_file() else None

    # ── Write API ────────────────────────────────────────────

    def upload(
        self,
        *,
        shortcode: str,
        mime: str,
        body_bytes: bytes,
        uploaded_by: str,
        description: str = "",
    ) -> CustomEmoji:
        shortcode = (shortcode or "").strip().lower()
        if not _SHORTCODE_RE.match(shortcode):
            raise CustomEmojiError(
                "shortcode must match [a-z0-9][a-z0-9_-]{1,30}",
            )
        if mime not in _ALLOWED_MIMES:
            raise CustomEmojiError(f"mime not allowed: {mime}")
        max_bytes = int(
            os.environ.get("HELEN_CUSTOM_EMOJI_MAX_BYTES",
                           str(256 * 1024)),
        )
        if len(body_bytes) > max_bytes:
            raise CustomEmojiError(
                f"file too large ({len(body_bytes)} > {max_bytes})",
            )

        with self._lock:
            self._load()
            # Sanity cap.
            if len(self._items) >= 1000:
                raise CustomEmojiError("too many custom emoji on server")
            # Uniqueness on shortcode.
            if any(
                e.shortcode == shortcode for e in self._items.values()
            ):
                raise CustomEmojiError(f"shortcode {shortcode!r} taken")

            emoji_id = hashlib.sha256(
                f"{shortcode}-{time.time()}-{uploaded_by}".encode("utf-8"),
            ).hexdigest()[:16]

            self.assets_dir.mkdir(parents=True, exist_ok=True)
            ext = _EXT_FOR_MIME[mime]
            (self.assets_dir / f"{emoji_id}.{ext}").write_bytes(body_bytes)

            e = CustomEmoji(
                id=emoji_id,
                shortcode=shortcode,
                mime=mime,
                size_bytes=len(body_bytes),
                uploaded_by=uploaded_by,
                uploaded_at=time.time(),
                description=description,
            )
            self._items[emoji_id] = e
            self._save()
            return e

    def delete(self, emoji_id: str) -> bool:
        with self._lock:
            self._load()
            e = self._items.pop(emoji_id, None)
            if e is None:
                return False
            ext = _EXT_FOR_MIME.get(e.mime, "bin")
            try:
                (self.assets_dir / f"{emoji_id}.{ext}").unlink(
                    missing_ok=True,
                )
            except OSError:
                pass
            self._save()
            return True


# ── Singleton ────────────────────────────────────────────────────


_store: Optional[_CustomEmojiStore] = None


def _get_store() -> _CustomEmojiStore:
    global _store
    if _store is None:
        try:
            from app.core.config import get_settings
            data_dir = (get_settings().PROJECT_ROOT / "data").resolve()
        except Exception:
            data_dir = Path("data")
        _store = _CustomEmojiStore(data_dir)
    return _store


def list_emoji() -> list[CustomEmoji]:
    return _get_store().list_all()


def get_emoji(emoji_id: str) -> Optional[CustomEmoji]:
    return _get_store().get(emoji_id)


def get_emoji_path(emoji_id: str) -> Optional[Path]:
    return _get_store().get_path(emoji_id)


def upload_emoji(**kw) -> CustomEmoji:
    return _get_store().upload(**kw)


def delete_emoji(emoji_id: str) -> bool:
    return _get_store().delete(emoji_id)


__all__ = [
    "CustomEmoji",
    "CustomEmojiError",
    "list_emoji", "get_emoji", "get_emoji_path",
    "upload_emoji", "delete_emoji",
]
