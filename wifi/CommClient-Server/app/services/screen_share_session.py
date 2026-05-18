"""Screen-sharing session tracker.

The mediasoup transport already carries the screen track — this
module just tracks *who is sharing what* per room so the client UX
can subscribe / unsubscribe + the SFU can apply a sticky-producer
flag (don't drop screen track on BWE pressure).
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ScreenShareSession:
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    room_id:    str = ""
    presenter:  str = ""             # user_id sharing
    track_id:   str = ""             # mediasoup producer id
    started_at: float = field(default_factory=time.time)
    last_pulse: float = field(default_factory=time.time)


class ScreenShareRegistry:
    _singleton: "ScreenShareRegistry | None" = None

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sessions: dict[str, ScreenShareSession] = {}  # by room_id

    @classmethod
    def instance(cls) -> "ScreenShareRegistry":
        if cls._singleton is None:
            cls._singleton = ScreenShareRegistry()
        return cls._singleton

    def start(self, room_id: str, presenter: str,
              track_id: str = "") -> ScreenShareSession:
        with self._lock:
            existing = self._sessions.get(room_id)
            if existing and existing.presenter == presenter:
                existing.last_pulse = time.time()
                if track_id:
                    existing.track_id = track_id
                return existing
            sess = ScreenShareSession(
                room_id=room_id, presenter=presenter, track_id=track_id,
            )
            self._sessions[room_id] = sess
        return sess

    def stop(self, room_id: str) -> bool:
        with self._lock:
            return self._sessions.pop(room_id, None) is not None

    def get(self, room_id: str) -> Optional[ScreenShareSession]:
        with self._lock:
            return self._sessions.get(room_id)

    def all_active(self) -> list[ScreenShareSession]:
        with self._lock:
            return list(self._sessions.values())

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "count": len(self._sessions),
                "sessions": [
                    {
                        "room_id":     s.room_id,
                        "presenter":   s.presenter,
                        "track_id":    s.track_id,
                        "session_id":  s.session_id,
                        "age_sec":     round(time.time() - s.started_at, 1),
                    }
                    for s in self._sessions.values()
                ],
            }


def get_screen_share_registry() -> ScreenShareRegistry:
    return ScreenShareRegistry.instance()
