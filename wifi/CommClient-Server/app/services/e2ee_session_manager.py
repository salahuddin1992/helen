"""E2EE session manager — per-(sender, recipient) state + key rotation.

The lower layer (E2EEService) handles identity keys + signed pre-keys
+ one-time pre-keys. This module sits above and tracks the *active
session* between two users:

  * session_id            — unique per pair, regenerated on rotation
  * established_at        — when the current session was opened
  * messages_in / out     — counters for ratchet rotation policy
  * last_pubkey_rotation  — when we last forced a fresh pre-key

Rotation policy:

  * Every ``HELEN_E2EE_ROTATE_MESSAGES`` messages (default 1000) OR
  * Every ``HELEN_E2EE_ROTATE_HOURS`` hours (default 24)

…the module marks the session for re-establishment. The next
outbound message triggers a fresh pre-key fetch.

In-memory only — surviving a process restart isn't required since
the next message will rebuild the session from key bundles in the
DB anyway.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from dataclasses import dataclass, field

from app.core.logging import get_logger

logger = get_logger(__name__)


def _i(env: str, default: int) -> int:
    try:
        return int(os.environ.get(env) or default)
    except (TypeError, ValueError):
        return default


ROTATE_MESSAGES = _i("HELEN_E2EE_ROTATE_MESSAGES", 1000)
ROTATE_HOURS    = _i("HELEN_E2EE_ROTATE_HOURS", 24)
# Idle eviction: a session that hasn't been used for IDLE_EVICT_HOURS
# is dropped from the registry. Without this, the in-memory dict grows
# unbounded — every (sender, recipient) pair the server ever sees stays
# resident. Re-establishment is cheap (key bundles live in the DB).
IDLE_EVICT_HOURS = _i("HELEN_E2EE_IDLE_EVICT_HOURS", 72)


@dataclass
class E2EESession:
    session_id:       str = field(default_factory=lambda: uuid.uuid4().hex)
    sender_id:        str = ""
    recipient_id:     str = ""
    established_at:   float = field(default_factory=time.time)
    last_used_at:     float = field(default_factory=time.time)
    messages_in:      int = 0
    messages_out:     int = 0
    last_rotation_at: float = field(default_factory=time.time)

    @property
    def key(self) -> tuple[str, str]:
        return (self.sender_id, self.recipient_id)

    def touch_in(self) -> None:
        self.messages_in += 1
        self.last_used_at = time.time()

    def touch_out(self) -> None:
        self.messages_out += 1
        self.last_used_at = time.time()

    def needs_rotation(self) -> bool:
        if self.messages_in + self.messages_out >= ROTATE_MESSAGES:
            return True
        age_hours = (time.time() - self.last_rotation_at) / 3600.0
        return age_hours >= ROTATE_HOURS

    def rotate(self) -> None:
        self.session_id = uuid.uuid4().hex
        self.last_rotation_at = time.time()
        self.messages_in = 0
        self.messages_out = 0


class E2EESessionRegistry:
    _singleton: "E2EESessionRegistry | None" = None

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sessions: dict[tuple[str, str], E2EESession] = {}

    @classmethod
    def instance(cls) -> "E2EESessionRegistry":
        if cls._singleton is None:
            cls._singleton = E2EESessionRegistry()
        return cls._singleton

    def get_or_open(self, sender_id: str, recipient_id: str) -> E2EESession:
        key = (sender_id, recipient_id)
        with self._lock:
            sess = self._sessions.get(key)
            if sess is None:
                sess = E2EESession(
                    sender_id=sender_id,
                    recipient_id=recipient_id,
                )
                self._sessions[key] = sess
            elif sess.needs_rotation():
                sess.rotate()
                logger.info(
                    "e2ee_session_rotated",
                    sender=sender_id[:16], recipient=recipient_id[:16],
                )
        return sess

    def close(self, sender_id: str, recipient_id: str) -> bool:
        with self._lock:
            return self._sessions.pop((sender_id, recipient_id), None) is not None

    def evict_idle(self, max_idle_seconds: float | None = None) -> int:
        """Drop sessions that haven't been used for the configured window.
        Returns the count of sessions evicted."""
        cutoff_secs = (
            float(max_idle_seconds) if max_idle_seconds is not None
            else IDLE_EVICT_HOURS * 3600.0
        )
        now = time.time()
        with self._lock:
            stale = [
                k for k, s in self._sessions.items()
                if (now - s.last_used_at) >= cutoff_secs
            ]
            for k in stale:
                self._sessions.pop(k, None)
        if stale:
            logger.info("e2ee_sessions_evicted", count=len(stale))
        return len(stale)

    def all_sessions(self) -> list[E2EESession]:
        with self._lock:
            return list(self._sessions.values())

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "count":             len(self._sessions),
                "rotate_messages":   ROTATE_MESSAGES,
                "rotate_hours":      ROTATE_HOURS,
                "idle_evict_hours":  IDLE_EVICT_HOURS,
                "sessions": [
                    {
                        "sender":         s.sender_id[:16],
                        "recipient":      s.recipient_id[:16],
                        "session_id":     s.session_id[:12],
                        "messages_in":    s.messages_in,
                        "messages_out":   s.messages_out,
                        "age_sec":        round(time.time() - s.established_at, 1),
                        "needs_rotation": s.needs_rotation(),
                    }
                    for s in self._sessions.values()
                ],
            }


def get_e2ee_session_registry() -> E2EESessionRegistry:
    return E2EESessionRegistry.instance()
