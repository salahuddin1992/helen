"""
Phase 6 / Module AB — retention scheduler.

The loop iterates every active ``RetentionPolicy`` and, for each, deletes
or anonymizes rows older than ``retention_days``.  Every action is recorded
in the audit-chain so the operator can prove compliance.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import delete as sa_delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_log
from app.core.logging import get_logger
from app.db.session import async_session_factory
from app.models.compliance import RetentionPolicy

logger = get_logger(__name__)


# Map entity_type → (module, class, timestamp column, anonymizable fields)
_ENTITY_MAP: Dict[str, tuple[str, str, str, tuple[str, ...]]] = {
    "messages":          ("app.models.message",           "Message",         "created_at", ("content",)),
    "audit_log":         ("app.models.audit_log",         "AuditLog",        "created_at", ()),
    "notifications":     ("app.models.notification",      "Notification",    "created_at", ()),
    "device_tokens":     ("app.models.device_token",      "DeviceToken",     "updated_at", ()),
    "saved_messages":    ("app.models.saved_message",     "SavedMessage",    "created_at", ()),
    "drafts":            ("app.models.message_draft",     "MessageDraft",    "updated_at", ()),
    "ai_messages":       ("app.models.ai_assistant",      "AIMessage",       "created_at", ("content",)),
    "ai_sessions":       ("app.models.ai_assistant",      "AISession",       "created_at", ()),
    "call_logs":         ("app.models.call_log",          "CallLog",         "created_at", ()),
    "voice_messages":    ("app.models.voice_message",     "VoiceMessage",    "created_at", ()),
    "data_exports":      ("app.models.compliance",        "DataExportRequest","requested_at", ()),
    "scheduled_messages":("app.models.scheduled_message", "ScheduledMessage","created_at", ()),
    "webhook_deliveries":("app.models.webhook_v2",        "WebhookDelivery", "created_at", ()),
}


def known_entity_types() -> List[str]:
    return list(_ENTITY_MAP.keys())


async def _apply_one(
    db: AsyncSession, policy: RetentionPolicy, dry_run: bool,
) -> Dict[str, Any]:
    entry = _ENTITY_MAP.get(policy.entity_type)
    if not entry:
        return {"entity_type": policy.entity_type, "skipped": "unknown entity"}
    module_path, class_name, ts_col, anon_fields = entry
    try:
        mod = __import__(module_path, fromlist=[class_name])
        cls = getattr(mod, class_name)
    except Exception as e:
        return {"entity_type": policy.entity_type, "skipped": str(e)}
    column = getattr(cls, ts_col, None)
    if column is None:
        return {"entity_type": policy.entity_type, "skipped": f"no column {ts_col}"}
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, policy.retention_days))
    from sqlalchemy import func
    n = (await db.execute(
        select(func.count()).select_from(cls).where(column < cutoff)
    )).scalar_one()
    if dry_run:
        return {"entity_type": policy.entity_type, "would_affect": int(n or 0)}
    affected = 0
    if policy.action == "delete":
        res = await db.execute(sa_delete(cls).where(column < cutoff))
        affected = int(res.rowcount or 0)
    elif policy.action == "anonymize":
        updates: Dict[str, Any] = {}
        for f in anon_fields:
            updates[f] = "[anonymized]"
        if updates:
            res = await db.execute(update(cls).where(column < cutoff).values(**updates))
            affected = int(res.rowcount or 0)
    policy.last_run_at = datetime.now(timezone.utc)
    policy.last_run_affected = affected
    return {"entity_type": policy.entity_type, "affected": affected, "action": policy.action}


async def run_pass(dry_run: bool = False) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    async with async_session_factory() as db:
        rows = (await db.execute(
            select(RetentionPolicy).where(RetentionPolicy.enabled.is_(True))
        )).scalars().all()
        for p in rows:
            try:
                rep = await _apply_one(db, p, dry_run=dry_run)
            except Exception as e:
                rep = {"entity_type": p.entity_type, "error": str(e)}
            out.append(rep)
        if not dry_run:
            await db.commit()
    audit_log("compliance.retention_pass", success=True,
              details={"results": out, "dry_run": dry_run})
    return out


# ── background scheduler ────────────────────────────────────────


@dataclass
class _State:
    enabled: bool = False
    interval_hours: float = 24.0
    last_run: Optional[datetime] = None
    run_count: int = 0
    last_results: List[Dict[str, Any]] = None         # type: ignore[assignment]


_state = _State()
_task: Optional[asyncio.Task] = None


def get_state() -> _State:
    return _state


async def _loop() -> None:
    await asyncio.sleep(60)
    while True:
        try:
            res = await run_pass(dry_run=False)
            _state.last_run = datetime.now(timezone.utc)
            _state.run_count += 1
            _state.last_results = res
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("retention_loop_failed", error=str(e))
        await asyncio.sleep(max(300.0, _state.interval_hours * 3600.0))


async def start(interval_hours: float = 24.0) -> None:
    global _task
    _state.enabled = True
    _state.interval_hours = float(interval_hours)
    if _task and not _task.done():
        return
    _task = asyncio.create_task(_loop(), name="compliance_retention_scheduler")


async def stop() -> None:
    global _task
    _state.enabled = False
    if _task and not _task.done():
        _task.cancel()
        try:
            await _task
        except (asyncio.CancelledError, Exception):
            pass
    _task = None


# ─────────────────────────────────────────────────────────────────
# Compliance Workbench v2 — advanced per-resource retention.
# Lives alongside the legacy scheduler above. The v2 surface honours
# legal holds, supports archive / redact_pii actions, and emits per-job
# rows in ``compliance_retention_jobs``.
# ─────────────────────────────────────────────────────────────────

import re
import uuid as _uuid_v2
from typing import Tuple as _Tuple_v2


_PII_PATTERNS: list[tuple[str, "re.Pattern[str]"]] = [
    ("email",       re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    ("phone",       re.compile(r"\b\+?\d[\d\s().-]{7,}\d\b")),
    ("ipv4",        re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
    ("creditcard",  re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    ("ssn",         re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("iban",        re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b")),
]


def _redact_pii(text: str) -> str:
    if not text:
        return text
    out = text
    for tag, rx in _PII_PATTERNS:
        out = rx.sub(f"[redacted:{tag}]", out)
    return out


class ComplianceRetentionService:
    """Advanced retention engine with hold awareness and richer actions."""

    async def list_policies(self, db: AsyncSession):
        from app.models.compliance_retention import ComplianceRetentionPolicy
        rows = (await db.execute(select(ComplianceRetentionPolicy))).scalars().all()
        return list(rows)

    async def get_policy(self, db: AsyncSession, policy_id: str):
        from app.models.compliance_retention import ComplianceRetentionPolicy
        return (await db.execute(
            select(ComplianceRetentionPolicy)
            .where(ComplianceRetentionPolicy.id == policy_id)
        )).scalar_one_or_none()

    async def create_policy(
        self, db: AsyncSession, *,
        name: str, resource_type: str, retention_days: int,
        action: str, selector: Dict[str, Any] | None = None,
        description: Optional[str] = None,
        respect_legal_hold: bool = True,
        enabled: bool = True,
        actor_id: str = "system",
    ):
        from app.models.compliance_retention import (
            VALID_ADV_RETENTION_ACTIONS,
            ComplianceRetentionPolicy,
        )
        if action not in VALID_ADV_RETENTION_ACTIONS:
            raise ValueError(f"action must be one of {VALID_ADV_RETENTION_ACTIONS}")
        p = ComplianceRetentionPolicy(
            id=_uuid_v2.uuid4().hex,
            name=name, description=description,
            resource_type=resource_type,
            selector=selector or {},
            retention_days=int(retention_days),
            action=action,
            enabled=enabled,
            respect_legal_hold=respect_legal_hold,
            created_by=actor_id,
        )
        db.add(p)
        await db.commit()
        audit_log("compliance.retention_policy_v2_created", user_id=actor_id,
                  success=True, details={"id": p.id, "resource_type": resource_type})
        return p

    async def update_policy(
        self, db: AsyncSession, policy_id: str, *,
        patch: Dict[str, Any], actor_id: str,
    ):
        p = await self.get_policy(db, policy_id)
        if p is None:
            raise LookupError(policy_id)
        for k, v in patch.items():
            if hasattr(p, k) and v is not None:
                setattr(p, k, v)
        await db.commit()
        audit_log("compliance.retention_policy_v2_updated", user_id=actor_id,
                  success=True, details={"id": policy_id})
        return p

    async def delete_policy(self, db: AsyncSession, policy_id: str, *, actor_id: str):
        p = await self.get_policy(db, policy_id)
        if p is None:
            raise LookupError(policy_id)
        await db.delete(p)
        await db.commit()
        audit_log("compliance.retention_policy_v2_deleted", user_id=actor_id,
                  success=True, details={"id": policy_id})

    async def preview(self, db: AsyncSession, policy_obj_or_dict: Any) -> Dict[str, Any]:
        """Count items that would be affected without making changes."""
        entity_type = (
            policy_obj_or_dict.get("resource_type")
            if isinstance(policy_obj_or_dict, dict)
            else policy_obj_or_dict.resource_type
        )
        retention_days = (
            policy_obj_or_dict.get("retention_days")
            if isinstance(policy_obj_or_dict, dict)
            else policy_obj_or_dict.retention_days
        )
        entry = _ENTITY_MAP.get(entity_type)
        if not entry:
            return {"resource_type": entity_type, "would_affect": 0,
                    "skipped": "unknown entity"}
        module_path, class_name, ts_col, _anon = entry
        try:
            mod = __import__(module_path, fromlist=[class_name])
            cls = getattr(mod, class_name)
        except Exception as e:
            return {"resource_type": entity_type, "would_affect": 0,
                    "skipped": str(e)}
        column = getattr(cls, ts_col, None)
        if column is None:
            return {"resource_type": entity_type, "would_affect": 0,
                    "skipped": f"no column {ts_col}"}
        from sqlalchemy import func as _f
        cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, int(retention_days)))
        n = (await db.execute(
            select(_f.count()).select_from(cls).where(column < cutoff)
        )).scalar_one()
        return {"resource_type": entity_type, "would_affect": int(n or 0),
                "cutoff": cutoff.isoformat()}

    async def apply(
        self,
        db: AsyncSession,
        policy_id: str,
        *,
        dry_run: bool,
        actor_id: str,
    ) -> Dict[str, Any]:
        from app.models.compliance_retention import (
            ComplianceRetentionJob,
            ComplianceRetentionPolicy,
        )
        from app.services.compliance.legal_holds import legal_holds_service

        p = await self.get_policy(db, policy_id)
        if p is None:
            raise LookupError(policy_id)
        job = ComplianceRetentionJob(
            id=_uuid_v2.uuid4().hex,
            policy_id=p.id,
            actor_id=actor_id,
            dry_run=dry_run,
            status="running",
        )
        db.add(job)
        await db.flush()

        entry = _ENTITY_MAP.get(p.resource_type)
        if not entry:
            job.status = "failed"
            job.error_message = f"unknown resource_type {p.resource_type}"
            job.finished_at = datetime.now(timezone.utc)
            await db.commit()
            return {"job_id": job.id, "status": "failed",
                    "error": job.error_message}

        module_path, class_name, ts_col, anon_fields = entry
        mod = __import__(module_path, fromlist=[class_name])
        cls = getattr(mod, class_name)
        column = getattr(cls, ts_col, None)

        cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, p.retention_days))
        rows = (await db.execute(
            select(cls).where(column < cutoff).limit(50000)
        )).scalars().all()

        affected = 0
        skipped_held = 0
        for row in rows:
            subject_id = (
                getattr(row, "sender_id", None)
                or getattr(row, "user_id", None)
                or getattr(row, "uploader_id", None)
            )
            channel_id = getattr(row, "channel_id", None)
            ts = getattr(row, ts_col, None)
            if p.respect_legal_hold:
                under, _ = await legal_holds_service.is_under_hold(
                    resource_type=p.resource_type,
                    resource_id=str(getattr(row, "id", "") or ""),
                    subject_id=subject_id,
                    timestamp=ts,
                    channel_id=channel_id,
                    db=db,
                )
                if under:
                    skipped_held += 1
                    continue

            if dry_run:
                affected += 1
                continue

            if p.action == "delete":
                await db.delete(row)
                affected += 1
            elif p.action == "anonymize":
                for f in anon_fields:
                    try:
                        setattr(row, f, "[anonymized]")
                    except Exception:
                        pass
                affected += 1
            elif p.action == "redact_pii":
                for f in anon_fields:
                    cur = getattr(row, f, None)
                    if isinstance(cur, str):
                        try:
                            setattr(row, f, _redact_pii(cur))
                        except Exception:
                            pass
                affected += 1
            elif p.action == "archive":
                if hasattr(row, "archived"):
                    try:
                        setattr(row, "archived", True)
                    except Exception:
                        pass
                affected += 1

        if not dry_run:
            p.last_run_at = datetime.now(timezone.utc)
            p.last_run_affected = affected
            p.last_run_skipped_held = skipped_held

        job.affected = affected
        job.skipped_held = skipped_held
        job.status = "ready"
        job.finished_at = datetime.now(timezone.utc)
        job.report = {
            "policy_id": p.id, "resource_type": p.resource_type,
            "action": p.action, "dry_run": dry_run,
            "affected": affected, "skipped_held": skipped_held,
        }
        await db.commit()
        audit_log("compliance.retention_policy_applied", user_id=actor_id,
                  success=True, details=job.report)
        return {"job_id": job.id, "status": "ready",
                "affected": affected, "skipped_held": skipped_held,
                "dry_run": dry_run}

    async def run_all(self, db: AsyncSession, *, actor_id: str) -> List[Dict[str, Any]]:
        from app.models.compliance_retention import ComplianceRetentionPolicy
        rows = (await db.execute(
            select(ComplianceRetentionPolicy)
            .where(ComplianceRetentionPolicy.enabled.is_(True))
        )).scalars().all()
        out: List[Dict[str, Any]] = []
        for p in rows:
            try:
                rep = await self.apply(db, p.id, dry_run=False, actor_id=actor_id)
            except Exception as e:
                rep = {"policy_id": p.id, "error": str(e)}
            out.append(rep)
        audit_log("compliance.retention_run_all", user_id=actor_id,
                  success=True, details={"jobs": out})
        return out


retention_service_v2 = ComplianceRetentionService()
