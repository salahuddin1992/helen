"""Peer session — short-lived authenticated context.

A "session" tracks an outbound conversation with a peer:

  * sequence number (caller supplies; we just track the highest seen)
  * created_at / last_used_at
  * how many requests have flowed through

Sessions are in-memory only; the audit chain captures durable
records when needed.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field

from app.p2p.peer_events import emit


@dataclass
class PeerSession:
    session_id:    str = field(default_factory=lambda: uuid.uuid4().hex)
    peer_id:       str = ""
    seq_high:      int = 0
    requests:      int = 0
    created_at:    float = field(default_factory=time.time)
    last_used_at:  float = field(default_factory=time.time)

    def record_request(self, seq: int = 0) -> None:
        self.requests += 1
        self.last_used_at = time.time()
        if seq > self.seq_high:
            self.seq_high = seq


class SessionManager:
    _singleton: "SessionManager | None" = None

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sessions: dict[str, PeerSession] = {}

    @classmethod
    def instance(cls) -> "SessionManager":
        if cls._singleton is None:
            cls._singleton = SessionManager()
        return cls._singleton

    def open(self, peer_id: str) -> PeerSession:
        with self._lock:
            existing = self._sessions.get(peer_id)
            if existing:
                return existing
            sess = PeerSession(peer_id=peer_id)
            self._sessions[peer_id] = sess
        emit("session.opened", {"peer_id": peer_id,
                                 "session_id": sess.session_id})
        return sess

    def close(self, peer_id: str) -> bool:
        with self._lock:
            sess = self._sessions.pop(peer_id, None)
        if sess:
            emit("session.closed", {"peer_id": peer_id,
                                     "session_id": sess.session_id})
            return True
        return False

    def get(self, peer_id: str) -> PeerSession | None:
        with self._lock:
            return self._sessions.get(peer_id)

    def all_sessions(self) -> list[PeerSession]:
        with self._lock:
            return list(self._sessions.values())

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "count": len(self._sessions),
                "sessions": [
                    {
                        "peer_id":      s.peer_id,
                        "session_id":   s.session_id,
                        "requests":     s.requests,
                        "age_sec":      round(time.time() - s.created_at, 1),
                        "idle_sec":     round(time.time() - s.last_used_at, 1),
                    }
                    for s in self._sessions.values()
                ],
            }


def get_session_manager() -> SessionManager:
    return SessionManager.instance()
