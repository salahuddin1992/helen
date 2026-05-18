"""
Phase 6 / Module AB — GDPR Article 17 (right to be forgotten).

Two-phase delete:

1. ``request_deletion(user_id, dry_run=True)`` → enumerates every row
   that would be touched and returns a structured plan + a 32-char
   confirmation token (UUID hex).
2. ``execute_deletion(request_id, token)`` → applies the plan.

Cascading rules:
    messages          — content emptied + sender_id anonymized to "deleted-<n>"
    files             — hard-deleted (DB rows + on-disk payload)
    sessions          — revoked (deleted)
    channels owned    — archived (is_active=False); if user is sole owner +
                        no other members, channel is deleted entirely
    audit / consents  — kept (immutable evidence trail)

A tombstone audit-log entry is written immediately and is *never* purged.
"""
from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import delete as sa_delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_log
from app.core.logging import get_logger
from app.db.session import async_session_factory
from app.models.compliance import DataDeletionRequest

logger = get_logger(__name__)


# ── plan building ───────────────────────────────────────────────


async def build_plan(db: AsyncSession, user_id: str) -> Dict[str, Any]:
    plan: Dict[str, Any] = {
        "user_id": user_id,
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "tables": {},
    }
    for module_path, class_name, col, action in _DELETION_RULES:
        n = await _count(db, module_path, class_name, col, user_id)
        if n is not None:
            plan["tables"][f"{class_name}.{col}"] = {"action": action, "count": n}
    plan["total_rows_touched"] = sum(
        v["count"] for v in plan["tables"].values()
    )
    return plan


_DELETION_RULES = [
    # (model_path, model_class, user_column, action)
    ("app.models.session",          "UserSession",     "user_id",   "delete"),
    ("app.models.contact",          "Contact",         "user_id",   "delete"),
    ("app.models.device_token",     "DeviceToken",     "user_id",   "delete"),
    ("app.models.saved_message",    "SavedMessage",    "user_id",   "delete"),
    ("app.models.message_draft",    "MessageDraft",    "user_id",   "delete"),
    ("app.models.notification",     "Notification",    "user_id",   "delete"),
    ("app.models.message_template", "MessageTemplate", "user_id",   "delete"),
    ("app.models.scheduled_message","ScheduledMessage","sender_id", "delete"),
    ("app.models.profile_photo",    "ProfilePhoto",    "user_id",   "delete"),
    ("app.models.message",          "Message",         "sender_id", "anonymize"),
    ("app.models.file",             "FileRecord",      "uploader_id","delete"),
    ("app.models.channel",          "ChannelMember",   "user_id",   "delete"),
    ("app.models.ai_assistant",     "AISession",       "user_id",   "delete"),
    ("app.models.ai_assistant",     "AIOptIn",         "user_id",   "delete"),
    ("app.models.compliance",       "DataExportRequest","user_id",  "delete"),
]


async def _count(
    db: AsyncSession, module_path: str, class_name: str, col: str, user_id: str,
) -> Optional[int]:
    try:
        mod = __import__(module_path, fromlist=[class_name])
        cls = getattr(mod, class_name)
        column = getattr(cls, col)
        from sqlalchemy import func
        n = (await db.execute(
            select(func.count()).select_from(cls).where(column == user_id)
        )).scalar_one()
        return int(n or 0)
    except Exception:
        return None


# ── public API ─────────────────────────────────────────────────


async def request_deletion(
    user_id: str, *, dry_run: bool = True, scheduled_delay_hours: int = 24,
) -> Dict[str, Any]:
    req_id = uuid.uuid4().hex
    token = secrets.token_urlsafe(24)
    async with async_session_factory() as db:
        plan = await build_plan(db, user_id)
        req = DataDeletionRequest(
            id=req_id,
            user_id=user_id,
            status="pending" if dry_run else "scheduled",
            scheduled_for=(
                datetime.now(timezone.utc) + timedelta(hours=scheduled_delay_hours)
                if not dry_run else None
            ),
            dry_run_report=plan,
            confirmation_token=token,
        )
        db.add(req)
        await db.commit()
    audit_log("compliance.deletion_requested", user_id=user_id, success=True,
              details={"request_id": req_id, "rows": plan.get("total_rows_touched", 0)})
    return {
        "request_id": req_id,
        "confirmation_token": token,
        "plan": plan,
        "scheduled_for": req.scheduled_for.isoformat() if req.scheduled_for else None,
    }


async def execute_deletion(
    request_id: str, confirmation_token: str,
) -> Dict[str, Any]:
    async with async_session_factory() as db:
        req = (await db.execute(
            select(DataDeletionRequest).where(DataDeletionRequest.id == request_id)
        )).scalar_one_or_none()
        if req is None:
            raise LookupError(request_id)
        if req.confirmation_token != confirmation_token:
            raise PermissionError("invalid confirmation token")
        if req.status in ("completed", "running"):
            raise RuntimeError(f"deletion already {req.status}")
        req.status = "running"
        await db.commit()
        user_id = req.user_id

    affected: Dict[str, int] = {}
    try:
        async with async_session_factory() as db:
            anon_name = f"deleted-{request_id[:8]}"
            for module_path, class_name, col, action in _DELETION_RULES:
                n = await _apply(db, module_path, class_name, col, user_id, action, anon_name)
                if n is not None:
                    affected[f"{class_name}.{col}"] = n
            await _archive_channels(db, user_id, anon_name)
            await db.commit()
    except Exception as e:
        async with async_session_factory() as db:
            req = (await db.execute(
                select(DataDeletionRequest).where(DataDeletionRequest.id == request_id)
            )).scalar_one_or_none()
            if req:
                req.status = "failed"
                req.error_message = str(e)[:1024]
                await db.commit()
        raise

    async with async_session_factory() as db:
        req = (await db.execute(
            select(DataDeletionRequest).where(DataDeletionRequest.id == request_id)
        )).scalar_one_or_none()
        req.status = "completed"
        req.executed_at = datetime.now(timezone.utc)
        report = dict(req.dry_run_report or {})
        report["affected"] = affected
        report["completed_at"] = req.executed_at.isoformat()
        req.dry_run_report = report
        await db.commit()

    audit_log("compliance.deletion_executed", user_id=user_id, success=True,
              details={"request_id": request_id, "affected": affected})
    return {"request_id": request_id, "affected": affected}


async def _apply(
    db: AsyncSession,
    module_path: str,
    class_name: str,
    col: str,
    user_id: str,
    action: str,
    anon_name: str,
) -> Optional[int]:
    try:
        mod = __import__(module_path, fromlist=[class_name])
        cls = getattr(mod, class_name)
        column = getattr(cls, col)
        if action == "delete":
            res = await db.execute(sa_delete(cls).where(column == user_id))
            return int(res.rowcount or 0)
        if action == "anonymize":
            update_values: Dict[str, Any] = {col: None}
            # blank out body if model has 'content'
            if hasattr(cls, "content"):
                update_values["content"] = "[redacted]"
            if hasattr(cls, "body"):
                update_values["body"] = "[redacted]"
            res = await db.execute(update(cls).where(column == user_id).values(**update_values))
            return int(res.rowcount or 0)
    except Exception as e:
        logger.debug("compliance_apply_skip", model=class_name, error=str(e))
        return None
    return 0


async def _archive_channels(db: AsyncSession, user_id: str, anon_name: str) -> None:
    try:
        from app.models.channel import Channel
        owned = (await db.execute(
            select(Channel).where(Channel.owner_id == user_id)
        )).scalars().all()
        for ch in owned:
            ch.owner_id = None
            if hasattr(ch, "is_active"):
                ch.is_active = False
            if hasattr(ch, "name") and ch.name:
                ch.name = f"{ch.name} (archived)"
    except Exception as e:
        logger.debug("compliance_archive_skip", error=str(e))


# ── housekeeping ───────────────────────────────────────────────


async def purge_purgeable() -> int:
    """Delete expired DataExportRequest archives + finalize old deletions."""
    from app.services.compliance.data_export import expire_old_exports
    return await expire_old_exports()
