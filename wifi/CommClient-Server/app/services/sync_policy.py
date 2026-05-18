"""
Runtime cluster-sync policy.

Default behaviour is "federate-first, ask-later": as soon as another
Helen-Server is reachable on any path (LAN, USB-tether bridge, fiber,
cross-router relay), it auto-federates. Operators control opt-out from
the admin panel without restarting:

  paused              — master kill-switch. When True, peer-acceptance
                        falls back to MANUAL_APPROVAL so newly
                        discovered peers park until an admin acts.
                        Existing peers stay federated.
  blocked_server_ids  — explicit per-peer blocklist. A peer in this
                        set is rejected at the federation HMAC gate
                        (403) regardless of approval status.

State is persisted to ``data/sync_policy.json`` so a Helen-Server
restart preserves operator decisions.

The policy is read on every federation gate call, so a flip in the
admin panel takes effect on the next request — no restart needed.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Optional

from app.core.logging import get_logger

logger = get_logger(__name__)


_DATA_DIR = Path(os.environ.get("COMMCLIENT_DATA_DIR",
                 str(Path(__file__).resolve().parents[2] / "data")))
_POLICY_FILE = _DATA_DIR / "sync_policy.json"


class SyncPolicy:
    """Singleton runtime policy. Thread-safe."""

    _singleton: "SyncPolicy | None" = None

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._paused: bool = False
        self._blocked: set[str] = set()
        self._loaded_at: float = 0.0
        self._load_from_disk()

    @classmethod
    def instance(cls) -> "SyncPolicy":
        if cls._singleton is None:
            cls._singleton = SyncPolicy()
        return cls._singleton

    def _load_from_disk(self) -> None:
        try:
            if _POLICY_FILE.is_file():
                data = json.loads(_POLICY_FILE.read_text(encoding="utf-8"))
                self._paused = bool(data.get("paused", False))
                blocked = data.get("blocked_server_ids") or []
                self._blocked = {str(x) for x in blocked if isinstance(x, str) and x}
                self._loaded_at = time.time()
                logger.info(
                    "sync_policy_loaded",
                    paused=self._paused,
                    blocked_count=len(self._blocked),
                )
        except Exception as e:
            logger.warning("sync_policy_load_failed", error=str(e))

    def _persist(self) -> None:
        try:
            _DATA_DIR.mkdir(parents=True, exist_ok=True)
            data = {
                "paused": self._paused,
                "blocked_server_ids": sorted(self._blocked),
                "updated_at": time.time(),
            }
            _POLICY_FILE.write_text(
                json.dumps(data, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning("sync_policy_persist_failed", error=str(e))

    @property
    def paused(self) -> bool:
        with self._lock:
            return self._paused

    def set_paused(self, paused: bool) -> dict:
        with self._lock:
            self._paused = bool(paused)
            self._persist()
            logger.info("sync_policy_pause_toggled", paused=self._paused)
            return self.snapshot_locked()

    def is_blocked(self, server_id: Optional[str]) -> bool:
        if not server_id:
            return False
        with self._lock:
            return server_id in self._blocked

    def block(self, server_id: str) -> dict:
        sid = (server_id or "").strip()
        if not sid:
            raise ValueError("server_id required")
        with self._lock:
            self._blocked.add(sid)
            self._persist()
            logger.info("sync_policy_peer_blocked", server_id=sid[:24])
            return self.snapshot_locked()

    def unblock(self, server_id: str) -> dict:
        sid = (server_id or "").strip()
        if not sid:
            raise ValueError("server_id required")
        with self._lock:
            removed = sid in self._blocked
            self._blocked.discard(sid)
            if removed:
                self._persist()
            logger.info(
                "sync_policy_peer_unblocked",
                server_id=sid[:24], was_blocked=removed,
            )
            return self.snapshot_locked()

    def snapshot(self) -> dict:
        with self._lock:
            return self.snapshot_locked()

    def snapshot_locked(self) -> dict:
        return {
            "paused": self._paused,
            "blocked_server_ids": sorted(self._blocked),
            "loaded_at": self._loaded_at,
        }


def get_sync_policy() -> SyncPolicy:
    return SyncPolicy.instance()
