"""Feature flags — per-cluster + per-user toggles with percentage rollout.

A flag is one of:

  * ``enabled: True/False``                        — global on/off
  * ``rollout_pct: 0..100``                        — deterministic % bucket
  * ``allowed_users: [...]``                        — explicit allow-list
  * ``blocked_users: [...]``                        — explicit deny-list

Resolution order (first match wins):

  1. blocked_users contains user_id   → False
  2. allowed_users contains user_id   → True
  3. rollout_pct > 0                  → hash(user_id) % 100 < rollout_pct
  4. enabled flag                     → its value

Flags are persisted via the existing replicated KV store
(``services.replication_manager``) so every peer agrees instantly.
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass, field
from typing import Any, Optional

from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass
class Flag:
    name:           str
    enabled:        bool = False
    rollout_pct:    int = 0
    allowed_users:  list[str] = field(default_factory=list)
    blocked_users:  list[str] = field(default_factory=list)
    description:    str = ""

    def is_active_for(self, user_id: str = "") -> bool:
        if user_id and user_id in self.blocked_users:
            return False
        if user_id and user_id in self.allowed_users:
            return True
        if self.rollout_pct > 0 and user_id:
            bucket = int(hashlib.sha256(
                f"{self.name}:{user_id}".encode()
            ).hexdigest()[:8], 16) % 100
            return bucket < self.rollout_pct
        return bool(self.enabled)

    def to_dict(self) -> dict:
        return {
            "name":          self.name,
            "enabled":       self.enabled,
            "rollout_pct":   self.rollout_pct,
            "allowed_users": list(self.allowed_users),
            "blocked_users": list(self.blocked_users),
            "description":   self.description,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Flag":
        return cls(
            name=str(data.get("name") or ""),
            enabled=bool(data.get("enabled", False)),
            rollout_pct=max(0, min(100, int(data.get("rollout_pct") or 0))),
            allowed_users=list(data.get("allowed_users") or []),
            blocked_users=list(data.get("blocked_users") or []),
            description=str(data.get("description") or ""),
        )


class FeatureFlagStore:
    _singleton: "FeatureFlagStore | None" = None

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._cache: dict[str, Flag] = {}

    @classmethod
    def instance(cls) -> "FeatureFlagStore":
        if cls._singleton is None:
            cls._singleton = FeatureFlagStore()
        return cls._singleton

    # ── Read ──────────────────────────────────────────────

    def get(self, name: str) -> Optional[Flag]:
        with self._lock:
            cached = self._cache.get(name)
        if cached is not None:
            return cached
        # Pull from replication manager.
        try:
            from app.services.replication_manager import get as rep_get
            rec = rep_get("flag", name)
            if rec and isinstance(rec.get("value"), dict):
                f = Flag.from_dict(rec["value"])
                with self._lock:
                    self._cache[name] = f
                return f
        except Exception:
            pass
        return None

    def is_active(self, name: str, user_id: str = "") -> bool:
        flag = self.get(name)
        if flag is None:
            return False
        return flag.is_active_for(user_id)

    # ── Write ─────────────────────────────────────────────

    def set(self, flag: Flag) -> Flag:
        if not flag.name:
            raise ValueError("flag.name required")
        try:
            from app.services.replication_manager import put as rep_put
            rep_put("flag", flag.name, flag.to_dict())
        except Exception as e:
            logger.warning("flag_persist_failed",
                           name=flag.name, error=str(e)[:80])
        with self._lock:
            self._cache[flag.name] = flag
        return flag

    def delete(self, name: str) -> bool:
        try:
            from app.services.replication_manager import put as rep_put
            rep_put("flag", name, {"name": name, "enabled": False,
                                    "rollout_pct": 0,
                                    "allowed_users": [],
                                    "blocked_users": []})
        except Exception:
            pass
        with self._lock:
            return self._cache.pop(name, None) is not None

    # ── Diagnostics ──────────────────────────────────────

    def list_known(self) -> list[dict]:
        with self._lock:
            return [f.to_dict() for f in self._cache.values()]

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "cached_flags": len(self._cache),
                "flags":        [f.to_dict() for f in self._cache.values()],
            }


def get_flag_store() -> FeatureFlagStore:
    return FeatureFlagStore.instance()


# ── Convenience top-level helper ───────────────────────────────


def is_active(name: str, user_id: str = "") -> bool:
    return get_flag_store().is_active(name, user_id)
