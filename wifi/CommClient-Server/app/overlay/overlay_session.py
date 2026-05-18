"""OverlaySession — TTL-bounded per-(overlay, src, dst) context.

A session represents an open conversation inside an overlay. We
track:

  * which path was last used (so we can re-use it before re-resolving)
  * how many messages have flowed
  * created_at / last_used_at (TTL eviction)

Sessions are in-memory only.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from app.overlay.overlay_config import get_config
from app.overlay.overlay_events import emit
from app.overlay.overlay_exceptions import OverlaySessionError
from app.overlay.overlay_route import OverlayRoute


@dataclass
class OverlaySession:
    session_id:    str = field(default_factory=lambda: uuid.uuid4().hex)
    overlay_name:  str = ""
    src_id:        str = ""
    dst_id:        str = ""
    last_route:    Optional[OverlayRoute] = None
    message_count: int = 0
    created_at:    float = field(default_factory=time.time)
    last_used_at:  float = field(default_factory=time.time)

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.overlay_name, self.src_id, self.dst_id)

    def touch(self) -> None:
        self.last_used_at = time.time()
        self.message_count += 1

    def is_expired(self, ttl_sec: float) -> bool:
        return (time.time() - self.last_used_at) > ttl_sec


class OverlaySessionManager:
    _singleton: "OverlaySessionManager | None" = None

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sessions: dict[tuple[str, str, str], OverlaySession] = {}

    @classmethod
    def instance(cls) -> "OverlaySessionManager":
        if cls._singleton is None:
            cls._singleton = OverlaySessionManager()
        return cls._singleton

    def open(self, overlay_name: str, src_id: str,
             dst_id: str) -> OverlaySession:
        if not overlay_name or not src_id or not dst_id:
            raise OverlaySessionError("overlay_name/src_id/dst_id required")
        key = (overlay_name, src_id, dst_id)
        with self._lock:
            existing = self._sessions.get(key)
            if existing is not None and not existing.is_expired(
                get_config().session_ttl_sec
            ):
                existing.touch()
                return existing
            sess = OverlaySession(
                overlay_name=overlay_name,
                src_id=src_id, dst_id=dst_id,
            )
            self._sessions[key] = sess
        emit("overlay.session.opened", {
            "overlay_name": overlay_name,
            "session_id":   sess.session_id,
        })
        return sess

    def close(self, overlay_name: str, src_id: str, dst_id: str) -> bool:
        key = (overlay_name, src_id, dst_id)
        with self._lock:
            sess = self._sessions.pop(key, None)
        if sess is None:
            return False
        emit("overlay.session.closed", {
            "overlay_name": overlay_name,
            "session_id":   sess.session_id,
        })
        return True

    def get(self, overlay_name: str, src_id: str,
            dst_id: str) -> Optional[OverlaySession]:
        with self._lock:
            return self._sessions.get((overlay_name, src_id, dst_id))

    def evict_expired(self) -> int:
        cfg = get_config()
        with self._lock:
            dead = [k for k, s in self._sessions.items()
                    if s.is_expired(cfg.session_ttl_sec)]
            for k in dead:
                self._sessions.pop(k, None)
        if dead:
            emit("overlay.session.evicted", {"count": len(dead)})
        return len(dead)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "count": len(self._sessions),
                "sessions": [
                    {
                        "overlay_name": s.overlay_name,
                        "session_id":   s.session_id,
                        "src_id":       s.src_id,
                        "dst_id":       s.dst_id,
                        "messages":     s.message_count,
                        "age_sec":      round(time.time() - s.created_at, 1),
                        "idle_sec":     round(time.time() - s.last_used_at, 1),
                    }
                    for s in self._sessions.values()
                ],
            }


def get_overlay_session_manager() -> OverlaySessionManager:
    return OverlaySessionManager.instance()
