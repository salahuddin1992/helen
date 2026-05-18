"""
External camera sources registry — lets the operator register IP cameras
or network streams that aren't already exposed via the OS webcam list.

Storage
-------
JSON file at ``<data>/camera_sources.json``. Each entry:

    {
      "id": "cam_<hex>",
      "name": "Front door",
      "url": "http://192.168.1.10/mjpg/video.mjpg",
      "type": "mjpeg" | "hls" | "webrtc-whip" | "other",
      "added_by": "<user_id>",
      "added_at": "2026-04-22T...Z",
      "last_checked": "2026-04-22T...Z",
      "reachable": true | false,
      "note": ""
    }

What types are supported end-to-end
-----------------------------------
* **mjpeg** — browsers render `<img src="<url>">` natively when the
  server returns `multipart/x-mixed-replace`. Zero backend work, works
  on every common IP camera (Hikvision, Dahua, Amcrest, etc.).
* **hls** — `<video src="<url>.m3u8">` plays directly in Chrome/Edge/
  Safari on the desktop. Requires the camera to output HLS.
* **webrtc-whip** — WHIP (WebRTC-HTTP Ingestion Protocol) URL. The
  client establishes a direct WebRTC session. Advanced cameras +
  MediaMTX / go2rtc expose this.
* **other** — stored verbatim; the client decides what to do.

Deliberately NOT supported today
--------------------------------
* **rtsp://** — needs an ffmpeg transcoder pipeline. Out of scope for
  this module; operators should run MediaMTX/go2rtc alongside and
  register the MJPEG or WHIP URL it exposes.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)
settings = get_settings()


_VALID_TYPES = frozenset({"mjpeg", "hls", "webrtc-whip", "other"})
_PROBE_TIMEOUT_SEC = 4.0


def _storage_path() -> Path:
    base = Path(settings.DATA_DIR) if hasattr(settings, "DATA_DIR") else Path("data")
    base.mkdir(parents=True, exist_ok=True)
    return base / "camera_sources.json"


class CameraSourceRegistry:
    """Thread-safe registry backed by a single JSON file on disk."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._path = _storage_path()
        self._sources: dict[str, dict[str, Any]] = {}
        self._load()

    # ── I/O ────────────────────────────────────────────
    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = self._path.read_text(encoding="utf-8")
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                self._sources = {s["id"]: s for s in parsed if isinstance(s, dict) and s.get("id")}
            elif isinstance(parsed, dict):
                self._sources = parsed
        except (OSError, ValueError) as e:
            logger.warning("camera_sources_load_failed", error=str(e))

    def _flush(self) -> None:
        data = list(self._sources.values())
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self._path)

    # ── Public API ─────────────────────────────────────
    def list_all(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._sources.values())

    def add(
        self,
        *,
        name: str,
        url: str,
        type_: str = "mjpeg",
        added_by: str = "",
        note: str = "",
    ) -> dict[str, Any]:
        if type_ not in _VALID_TYPES:
            raise ValueError(f"type must be one of {sorted(_VALID_TYPES)}")
        if not url or not url.startswith(("http://", "https://")):
            raise ValueError("url must be http:// or https:// (RTSP not supported here)")
        entry = {
            "id": "cam_" + secrets.token_hex(6),
            "name": name.strip() or "Unnamed camera",
            "url": url.strip(),
            "type": type_,
            "added_by": added_by,
            "added_at": datetime.now(timezone.utc).isoformat(),
            "last_checked": None,
            "reachable": None,
            "note": note,
        }
        with self._lock:
            self._sources[entry["id"]] = entry
            self._flush()
        return entry

    def remove(self, camera_id: str) -> bool:
        with self._lock:
            removed = self._sources.pop(camera_id, None) is not None
            if removed:
                self._flush()
            return removed

    def test_url(self, camera_id: str) -> dict[str, Any]:
        with self._lock:
            entry = self._sources.get(camera_id)
            if not entry:
                return {"ok": False, "error": "not found"}
            url = entry["url"]
            ctype_expected = entry["type"]

        # HEAD first (cheap); fall back to GET with a 2-byte read so we don't
        # drain multi-megabyte streams just to confirm reachability.
        ok = False
        reason = ""
        content_type = ""
        t0 = time.monotonic()
        try:
            req = urllib.request.Request(url, method="HEAD")
            with urllib.request.urlopen(req, timeout=_PROBE_TIMEOUT_SEC) as resp:
                content_type = resp.headers.get("Content-Type", "")
                ok = 200 <= resp.status < 400
        except (urllib.error.HTTPError, urllib.error.URLError, OSError):
            # Some MJPEG servers 405 on HEAD; retry with GET + small read.
            try:
                req = urllib.request.Request(url, method="GET")
                with urllib.request.urlopen(req, timeout=_PROBE_TIMEOUT_SEC) as resp:
                    content_type = resp.headers.get("Content-Type", "")
                    resp.read(2)  # just confirm bytes flow
                    ok = True
            except Exception as e:
                reason = f"{type(e).__name__}: {e}"
        rtt_ms = int((time.monotonic() - t0) * 1000)

        # Light content-type sanity: MJPEG should return multipart/*;
        # HLS should advertise application/vnd.apple.mpegurl or text/plain.
        type_hint = ""
        if content_type:
            lc = content_type.lower()
            if "multipart" in lc:
                type_hint = "mjpeg"
            elif "mpegurl" in lc or lc.startswith("application/vnd.apple.mpegurl"):
                type_hint = "hls"

        with self._lock:
            entry = self._sources.get(camera_id)
            if entry is not None:
                entry["last_checked"] = datetime.now(timezone.utc).isoformat()
                entry["reachable"] = ok
                self._flush()

        return {
            "ok": ok,
            "rtt_ms": rtt_ms,
            "content_type": content_type,
            "type_hint": type_hint,
            "declared_type": ctype_expected,
            "error": reason or None,
        }


camera_sources = CameraSourceRegistry()
