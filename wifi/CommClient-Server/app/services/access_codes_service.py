"""
User-generated access codes — file-backed store.

Each regular user can mint short codes that act as:
  - invite: lets another user join a specific channel
  - guest_auth: one-time/short-TTL auth token that creates a guest session
  - share: binds to the user's profile (like a share-code variant with TTL)

Codes are 8-char uppercase alphanumeric (no confusing chars), user-scoped,
and persisted to data/access_codes.json. Redemption decrements
`uses_remaining`; codes expire on TTL or exhaustion.

Not using a DB migration deliberately — this keeps the feature optional
and easy to disable by deleting the file. Under heavy load the service
flushes debounced (every 1s or on shutdown) rather than per-mutation.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import string
import threading
import time
from pathlib import Path
from typing import Any, Optional

import structlog

logger = structlog.get_logger(__name__)

_DATA_DIR = Path(__file__).resolve().parents[2] / "data"
_STORE_FILE = _DATA_DIR / "access_codes.json"

# 24 chars = 32 bits of entropy when picking 8 — collision-safe at our scale.
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

VALID_KINDS = {"invite", "guest_auth", "share"}


def _gen_code(length: int = 8) -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(length))


class AccessCodesService:
    _singleton: "AccessCodesService | None" = None

    def __init__(self) -> None:
        self._codes: dict[str, dict[str, Any]] = {}  # code → record
        self._lock = threading.RLock()
        self._dirty = False
        self._load()

    @classmethod
    def instance(cls) -> "AccessCodesService":
        if cls._singleton is None:
            cls._singleton = AccessCodesService()
        return cls._singleton

    def _load(self) -> None:
        try:
            if _STORE_FILE.is_file():
                raw = json.loads(_STORE_FILE.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and "codes" in raw:
                    self._codes = raw["codes"]
                elif isinstance(raw, list):
                    # legacy format
                    self._codes = {c["code"]: c for c in raw}
        except Exception as e:
            logger.warning("access_codes_load_failed", error=str(e))
            self._codes = {}

    def _save(self) -> None:
        try:
            _STORE_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = _STORE_FILE.with_suffix(".tmp")
            tmp.write_text(
                json.dumps({"codes": self._codes}, indent=2),
                encoding="utf-8",
            )
            tmp.replace(_STORE_FILE)
            self._dirty = False
        except Exception as e:
            logger.warning("access_codes_save_failed", error=str(e))

    def _flush_if_dirty(self) -> None:
        with self._lock:
            if self._dirty:
                self._save()

    # ── Public API ─────────────────────────────────────────────
    def create(
        self,
        owner_user_id: str,
        kind: str,
        note: str = "",
        max_uses: Optional[int] = None,
        ttl_sec: Optional[int] = None,
        target_channel_id: Optional[str] = None,
    ) -> dict[str, Any]:
        if kind not in VALID_KINDS:
            raise ValueError(f"kind must be one of {sorted(VALID_KINDS)}")
        if max_uses is not None and max_uses <= 0:
            raise ValueError("max_uses must be positive")
        if ttl_sec is not None and ttl_sec <= 0:
            raise ValueError("ttl_sec must be positive")

        now = time.time()
        # Reject duplicates (tiny chance, but deterministic).
        for _ in range(8):
            code = _gen_code(8)
            if code not in self._codes:
                break
        else:
            raise RuntimeError("Could not allocate unique code; storage full")

        record = {
            "code":              code,
            "owner_user_id":     owner_user_id,
            "kind":              kind,
            "note":              (note or "")[:200],
            "max_uses":          max_uses,
            "uses_remaining":    max_uses,
            "used_count":        0,
            "target_channel_id": target_channel_id,
            "created_at":        now,
            "expires_at":        (now + ttl_sec) if ttl_sec else None,
            "revoked":           False,
        }
        with self._lock:
            self._codes[code] = record
            self._dirty = True
            self._save()
        logger.info("access_code_created",
                    code=code, owner=owner_user_id, kind=kind)
        return self._redact(record)

    def list_by_owner(self, owner_user_id: str) -> list[dict]:
        with self._lock:
            rows = [r for r in self._codes.values()
                    if r.get("owner_user_id") == owner_user_id]
        rows.sort(key=lambda r: r["created_at"], reverse=True)
        return [self._redact(r) for r in rows]

    def list_all(self) -> list[dict]:
        """Admin-only: all codes system-wide."""
        with self._lock:
            rows = list(self._codes.values())
        rows.sort(key=lambda r: r["created_at"], reverse=True)
        return [self._redact(r, admin=True) for r in rows]

    def revoke(self, code: str, by_user_id: str) -> bool:
        with self._lock:
            r = self._codes.get(code.upper())
            if not r or r.get("revoked"):
                return False
            if r["owner_user_id"] != by_user_id:
                raise PermissionError("not the owner of this code")
            r["revoked"] = True
            self._dirty = True
            self._save()
        logger.info("access_code_revoked", code=code, by=by_user_id)
        return True

    def admin_revoke(self, code: str, by_user_id: str) -> bool:
        """Force-revoke ignoring ownership. Used by secret admin."""
        with self._lock:
            r = self._codes.get(code.upper())
            if not r:
                return False
            r["revoked"] = True
            r["_admin_revoked_by"] = by_user_id
            r["_admin_revoked_at"] = time.time()
            self._dirty = True
            self._save()
        return True

    def redeem(self, code: str, redeemer_user_id: str
               ) -> tuple[bool, str, Optional[dict]]:
        """Redeem a code on behalf of `redeemer_user_id`.

        Returns (success, reason, record_or_None). `reason` is a short
        machine-readable string on failure: expired, revoked, exhausted,
        not_found, self_redeem_forbidden.
        """
        with self._lock:
            r = self._codes.get(code.strip().upper())
            if not r:
                return False, "not_found", None
            now = time.time()
            if r.get("revoked"):
                return False, "revoked", None
            if r.get("expires_at") and r["expires_at"] < now:
                return False, "expired", None
            if r.get("max_uses") is not None and r.get("uses_remaining", 0) <= 0:
                return False, "exhausted", None
            if r["owner_user_id"] == redeemer_user_id and r["kind"] != "guest_auth":
                return False, "self_redeem_forbidden", None
            r["used_count"] = r.get("used_count", 0) + 1
            if r.get("max_uses") is not None:
                r["uses_remaining"] = max(0, r["uses_remaining"] - 1)
            r.setdefault("redemption_log", []).append({
                "by":  redeemer_user_id,
                "at":  now,
            })
            # Trim redemption log to last 50 entries.
            r["redemption_log"] = r["redemption_log"][-50:]
            self._dirty = True
            self._save()
        logger.info("access_code_redeemed",
                    code=code, by=redeemer_user_id, kind=r["kind"])
        return True, "ok", self._redact(r)

    def lookup_redacted(self, code: str) -> Optional[dict]:
        """Read-only resolution of a code. Used by the browser
        ``/join/{code}`` landing page to render channel context
        WITHOUT incrementing the redeem counter. Returns the same
        redacted dict shape ``redeem`` returns on success, plus a
        ``revoked`` flag so callers can render an "this link was
        cancelled" message; ``None`` when the code doesn't exist."""
        with self._lock:
            r = self._codes.get(code.strip().upper())
            if not r:
                return None
            out = self._redact(r)
            out["revoked"] = bool(r.get("revoked"))
            return out

    # ── Helpers ────────────────────────────────────────────────
    @staticmethod
    def _redact(r: dict, admin: bool = False) -> dict:
        # Present a stable shape to callers; admin view includes more.
        out = {
            "code":              r["code"],
            "kind":              r["kind"],
            "note":              r.get("note", ""),
            "created_at":        r.get("created_at"),
            "expires_at":        r.get("expires_at"),
            "max_uses":          r.get("max_uses"),
            "uses_remaining":    r.get("uses_remaining"),
            "used_count":        r.get("used_count", 0),
            "target_channel_id": r.get("target_channel_id"),
            "revoked":           bool(r.get("revoked")),
        }
        if admin:
            out["owner_user_id"] = r.get("owner_user_id")
            out["redemption_log"] = r.get("redemption_log", [])
            out["_admin_revoked_by"] = r.get("_admin_revoked_by")
        return out


# Convenience singleton accessor for route modules.
def get_service() -> AccessCodesService:
    return AccessCodesService.instance()
