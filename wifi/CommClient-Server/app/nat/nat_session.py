"""NAT session — TTL-bounded record of an in-flight traversal attempt.

Tracks per-peer:
  * which strategy succeeded (and which failed)
  * the established public endpoint
  * created_at / last_used_at (for TTL eviction)

Allows the manager to short-circuit subsequent ``traverse(peer)``
calls — if we already know UDP punch worked 30 seconds ago, reuse
the result instead of re-running the ladder.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from app.nat.nat_config import get_config
from app.nat.nat_events import emit
from app.nat.nat_exceptions import NATSessionError


@dataclass
class NATSession:
    session_id:       str = field(default_factory=lambda: uuid.uuid4().hex)
    peer_id:          str = ""
    strategy:         str = ""
    public_endpoint:  Optional[tuple[str, int]] = None
    success:          bool = False
    last_error:       str = ""
    created_at:       float = field(default_factory=time.time)
    last_used_at:     float = field(default_factory=time.time)

    def touch(self) -> None:
        self.last_used_at = time.time()

    def is_expired(self, ttl_sec: float) -> bool:
        return (time.time() - self.last_used_at) > ttl_sec


class NATSessionManager:
    _singleton: "NATSessionManager | None" = None

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sessions: dict[str, NATSession] = {}

    @classmethod
    def instance(cls) -> "NATSessionManager":
        if cls._singleton is None:
            cls._singleton = NATSessionManager()
        return cls._singleton

    # ── CRUD ──────────────────────────────────────────────

    def get(self, peer_id: str) -> Optional[NATSession]:
        cfg = get_config()
        with self._lock:
            sess = self._sessions.get(peer_id)
            if sess and sess.is_expired(cfg.session_ttl_sec):
                self._sessions.pop(peer_id, None)
                return None
            return sess

    def open(self, peer_id: str, strategy: str,
             public_endpoint: Optional[tuple[str, int]] = None,
             *, success: bool = False,
             last_error: str = "") -> NATSession:
        cfg = get_config()
        if not peer_id:
            raise NATSessionError("peer_id required")
        with self._lock:
            if len(self._sessions) >= cfg.session_max:
                # Evict the oldest.
                oldest = min(self._sessions.values(),
                             key=lambda s: s.last_used_at)
                self._sessions.pop(oldest.peer_id, None)
            sess = NATSession(
                peer_id=peer_id, strategy=strategy,
                public_endpoint=public_endpoint,
                success=success, last_error=last_error,
            )
            self._sessions[peer_id] = sess
        emit("nat.session_opened", {
            "peer_id":   peer_id,
            "strategy":  strategy,
            "success":   success,
        })
        return sess

    def close(self, peer_id: str) -> bool:
        with self._lock:
            removed = self._sessions.pop(peer_id, None) is not None
        if removed:
            emit("nat.session_closed", {"peer_id": peer_id})
        return removed

    def evict_expired(self) -> int:
        cfg = get_config()
        with self._lock:
            dead = [k for k, s in self._sessions.items()
                    if s.is_expired(cfg.session_ttl_sec)]
            for k in dead:
                self._sessions.pop(k, None)
        return len(dead)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "count": len(self._sessions),
                "sessions": [
                    {
                        "peer_id":         s.peer_id,
                        "strategy":        s.strategy,
                        "public_endpoint": (f"{s.public_endpoint[0]}:{s.public_endpoint[1]}"
                                            if s.public_endpoint else None),
                        "success":         s.success,
                        "age_sec":         round(time.time() - s.created_at, 1),
                        "idle_sec":        round(time.time() - s.last_used_at, 1),
                    }
                    for s in self._sessions.values()
                ],
            }


def get_session_manager() -> NATSessionManager:
    return NATSessionManager.instance()
