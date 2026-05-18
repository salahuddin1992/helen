"""
Security audit logging.

Records security-relevant events with structured context for forensic analysis.
Events: login, logout, token_refresh, permission_denied, rate_limited,
        call_signal_unauthorized, file_access, account_locked.

Two destinations:
  1. Structured logger (always — synchronous, stdout/JSON)
  2. Persistent audit_logs table (best-effort — async background task)
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

from app.core.logging import get_logger

# Dedicated audit logger — separate from application logs
_audit_logger = get_logger("security.audit")

# Best-effort in-memory queue for DB persistence. Drained by a background
# coroutine started from main.py's lifespan handler.
_pending_entries: asyncio.Queue[dict[str, Any]] | None = None
_writer_task: asyncio.Task | None = None


def _get_queue() -> asyncio.Queue[dict[str, Any]] | None:
    """Get the queue if a running loop is available, else None (sync context)."""
    global _pending_entries
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    if _pending_entries is None:
        _pending_entries = asyncio.Queue(maxsize=10000)
    return _pending_entries


async def _audit_writer_loop() -> None:
    """
    Background coroutine: drains the queue and persists entries to DB.
    Started by app lifespan; cancelled on shutdown.
    """
    from app.db.session import async_session_factory
    from app.models.audit_log import AuditLog

    queue = _get_queue()
    if queue is None:
        return

    while True:
        entry = await queue.get()
        try:
            async with async_session_factory() as db:
                row = AuditLog(
                    event=entry["event"],
                    user_id=entry.get("user_id") or "anonymous",
                    ip_address=entry.get("ip_address") or "unknown",
                    success=bool(entry.get("success", True)),
                    details_json=(
                        json.dumps(entry["details"]) if entry.get("details") else None
                    ),
                )
                # Allow caller to override timestamp
                if entry.get("occurred_at"):
                    try:
                        row.occurred_at = entry["occurred_at"]
                    except Exception:
                        pass
                db.add(row)
                await db.commit()
        except Exception as e:
            # Never let DB issues kill the writer loop — keep draining.
            _audit_logger.error("audit_db_write_failed", error=str(e), event=entry.get("event"))
        finally:
            queue.task_done()


async def start_audit_writer() -> None:
    """Start the background DB writer (called from main.py lifespan)."""
    global _writer_task
    if _writer_task and not _writer_task.done():
        return
    _writer_task = asyncio.create_task(_audit_writer_loop(), name="audit_writer")


async def stop_audit_writer() -> None:
    """Cancel the background writer (called from main.py shutdown)."""
    global _writer_task
    if _writer_task and not _writer_task.done():
        _writer_task.cancel()
        try:
            await _writer_task
        except (asyncio.CancelledError, Exception):
            pass
    _writer_task = None


def audit_log(
    event: str,
    user_id: str | None = None,
    ip_address: str | None = None,
    success: bool = True,
    details: dict[str, Any] | None = None,
) -> None:
    """
    Write a structured security audit entry.
    Always logs; persists to DB if a writer task is active.
    """
    now = datetime.now(timezone.utc)
    entry: dict[str, Any] = {
        "audit_event": event,
        "event": event,
        "timestamp": now.isoformat(),
        "occurred_at": now,
        "user_id": user_id or "anonymous",
        "ip_address": ip_address or "unknown",
        "success": success,
    }
    if details:
        entry["details"] = details

    # Build kwargs for structlog — exclude `event` and `occurred_at` since
    # structlog uses `event` as the positional message argument.
    log_kwargs = {
        k: v for k, v in entry.items() if k not in ("event", "occurred_at")
    }
    if success:
        _audit_logger.info(event, **log_kwargs)
    else:
        _audit_logger.warning(event, **log_kwargs)

    # Best-effort enqueue for DB persistence
    queue = _get_queue()
    if queue is not None:
        try:
            queue.put_nowait(entry)
        except asyncio.QueueFull:
            _audit_logger.warning("audit_queue_full_dropping_entry", dropped_event=event)

    # Best-effort: also append to the tamper-evident hash chain so any
    # later edit/deletion of the SQLite audit_logs table is detectable.
    # The chain lives in a separate SQLite file; if it's not yet
    # configured (process startup race) we silently no-op.
    try:
        from app.services.audit_chain import get_audit_chain
        chain = get_audit_chain()
        if chain is not None:
            target = None
            if details and isinstance(details, dict):
                target = (details.get("target") or details.get("file_id")
                          or details.get("resource") or details.get("event"))
            chain.append(
                actor=user_id or "anonymous",
                action=event,
                target=str(target) if target else None,
                payload={
                    "ip": ip_address or "unknown",
                    "success": bool(success),
                    "details": details or {},
                },
            )
    except Exception as _ce:
        # Don't let chain failure mask the original audit event.
        _audit_logger.debug("audit_chain_append_failed",
                            error=str(_ce), event=event)


# ── Convenience functions ────────────────────────────────

def audit_login(user_id: str, ip: str, success: bool, reason: str = "") -> None:
    audit_log("auth.login", user_id=user_id, ip_address=ip, success=success,
              details={"reason": reason} if reason else None)


def audit_logout(user_id: str, ip: str = "") -> None:
    audit_log("auth.logout", user_id=user_id, ip_address=ip)


def audit_token_refresh(user_id: str, ip: str = "", success: bool = True) -> None:
    audit_log("auth.token_refresh", user_id=user_id, ip_address=ip, success=success)


def audit_permission_denied(
    user_id: str, resource: str, action: str, ip: str = ""
) -> None:
    audit_log(
        "authz.denied",
        user_id=user_id,
        ip_address=ip,
        success=False,
        details={"resource": resource, "action": action},
    )


def audit_rate_limited(user_id: str, event: str, ip: str = "") -> None:
    audit_log(
        "security.rate_limited",
        user_id=user_id,
        ip_address=ip,
        success=False,
        details={"event": event},
    )


def audit_file_access(user_id: str, file_id: str, action: str, ip: str = "") -> None:
    audit_log(
        "file.access",
        user_id=user_id,
        ip_address=ip,
        details={"file_id": file_id, "action": action},
    )


def audit_call_signal_unauthorized(
    user_id: str, target_id: str, signal_type: str
) -> None:
    audit_log(
        "call.signal_unauthorized",
        user_id=user_id,
        success=False,
        details={"target_id": target_id, "signal_type": signal_type},
    )


def audit_account_locked(username: str, ip: str, reason: str) -> None:
    audit_log(
        "auth.account_locked",
        user_id=username,
        ip_address=ip,
        success=False,
        details={"reason": reason},
    )


def audit_rbac_denied(
    user_id: str, user_role: str, required_role: str, endpoint: str = ""
) -> None:
    """Log RBAC access denial."""
    audit_log(
        "authz.rbac_denied",
        user_id=user_id,
        success=False,
        details={
            "user_role": user_role,
            "required_role": required_role,
            "endpoint": endpoint,
        },
    )


def audit_channel_access_denied(
    user_id: str, channel_id: str, action: str
) -> None:
    """Log unauthorized channel access attempt."""
    audit_log(
        "authz.channel_denied",
        user_id=user_id,
        success=False,
        details={"channel_id": channel_id, "action": action},
    )


def audit_admin_action(
    admin_id: str, action: str, target_id: str = "", details: dict | None = None
) -> None:
    """Log admin actions for accountability trail."""
    audit_log(
        f"admin.{action}",
        user_id=admin_id,
        success=True,
        details={"target_id": target_id, **(details or {})},
    )


def audit_security_event(
    event_name: str, user_id: str = "", details: dict | None = None
) -> None:
    """Log general security events (connection rejected, malformed input, etc.)."""
    audit_log(
        f"security.{event_name}",
        user_id=user_id or "anonymous",
        success=False,
        details=details,
    )
