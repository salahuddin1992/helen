"""
RouterAuditHook — wrap write ops on the router proxy with audit
log entries.

Each forwarded write call produces TWO chained audit entries:

  1. **Intent** — captured before the network call so a hung
     router still appears in the audit chain. Event suffix:
     ``.attempt`` (e.g. ``router.post.attempt``).
  2. **Outcome** — captured after the network call, carrying the
     HTTP status, latency, and any error. Event suffix:
     ``.result``.

The two entries share a ``correlation_id`` so analysts can
join them. Bodies of write ops are summarised (truncated +
keys-only redaction for the obvious secret-bearing fields).

Audit log itself is delegated to :func:`app.core.audit.audit_log`
which already writes to:

  * structlog (stdout / file),
  * the SQLite ``audit_logs`` table,
  * the tamper-evident hash chain.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from app.core.audit import audit_log
from app.core.logging import get_logger

logger = get_logger(__name__)


# Field names whose VALUES we never want to write to the audit
# log even if the operator passes them in plaintext. We DO log
# the key (so analysts know which field changed) but value is
# masked.
_REDACT_KEYS: frozenset[str] = frozenset({
    "token", "secret", "password", "api_key", "apikey",
    "credential", "credentials", "private_key", "ssh_key",
    "authorization", "auth", "key",
})

# Maximum bytes of body we keep in the audit payload — keeps the
# audit_logs table from ballooning when an admin pushes a fat
# config blob.
_MAX_BODY_BYTES = 4 * 1024


# ── Public dataclass ─────────────────────────────────────────────


@dataclass(frozen=True)
class _AuditToken:
    """Returned by :meth:`RouterAuditHook.before` — pass to
    :meth:`after` to chain the two halves."""

    correlation_id: str
    event: str
    user_id: str


# ── The hook ─────────────────────────────────────────────────────


class RouterAuditHook:
    """Emit before/after audit entries for router proxy ops."""

    async def before(
        self, *,
        event: str,
        user_id: str,
        method: str,
        path: str,
        query: Mapping[str, Any] | None,
        body: bytes | None,
        client_ip: str,
    ) -> _AuditToken:
        """Log the intent of a write op. Returns a token that
        :meth:`after` consumes to record the outcome."""
        correlation_id = secrets.token_hex(8)
        try:
            audit_log(
                event=f"{event}.attempt",
                user_id=user_id,
                ip_address=client_ip,
                success=True,
                details={
                    "correlation_id": correlation_id,
                    "method": method,
                    "path": path,
                    "query": dict(query) if query else None,
                    "body": self._summarize_body(body),
                    "target": "helen-router",
                },
            )
        except Exception as exc:                # pragma: no cover
            logger.debug("router_audit_before_failed", error=str(exc))
        return _AuditToken(
            correlation_id=correlation_id,
            event=event,
            user_id=user_id,
        )

    async def after(
        self,
        token: _AuditToken,
        *,
        success: bool,
        status_code: int,
        elapsed_ms: float = 0.0,
        error: str | None = None,
    ) -> None:
        """Log the outcome of a previously-started op."""
        try:
            audit_log(
                event=f"{token.event}.result",
                user_id=token.user_id,
                success=success,
                details={
                    "correlation_id": token.correlation_id,
                    "status_code": status_code,
                    "elapsed_ms": round(elapsed_ms, 2),
                    "error": error,
                    "target": "helen-router",
                },
            )
        except Exception as exc:                # pragma: no cover
            logger.debug("router_audit_after_failed", error=str(exc))

    # ── Body summarisation ─────────────────────────────────────

    @classmethod
    def _summarize_body(cls, body: bytes | None) -> Any:
        """Produce a redacted, length-capped projection of the
        request body suitable for the audit payload."""
        if not body:
            return None
        if len(body) > _MAX_BODY_BYTES:
            return {
                "redacted_reason": "body_too_large",
                "size_bytes": len(body),
            }
        try:
            parsed = json.loads(body.decode("utf-8"))
        except Exception:
            return {
                "redacted_reason": "non_json",
                "size_bytes": len(body),
            }
        return cls._redact(parsed)

    @classmethod
    def _redact(cls, obj: Any) -> Any:
        if isinstance(obj, Mapping):
            out: dict[str, Any] = {}
            for k, v in obj.items():
                if isinstance(k, str) and k.lower() in _REDACT_KEYS:
                    out[k] = "***REDACTED***"
                else:
                    out[k] = cls._redact(v)
            return out
        if isinstance(obj, list):
            return [cls._redact(x) for x in obj]
        if isinstance(obj, tuple):
            return tuple(cls._redact(x) for x in obj)
        return obj
