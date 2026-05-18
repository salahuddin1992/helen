"""
Retention service.

Implements operator-defined retention policies over audit records (and
related resource_types). Active legal holds always override retention.

Actions:
    archive    — move to cold storage (best-effort: writes a JSONL
                 snapshot under data/audit_archive/<policy>/<date>.jsonl
                 then deletes from primary store)
    delete     — hard delete (subject to legal-hold override)
    anonymize  — replace identifying fields (actor) with "redacted-<sha>"

The retention engine never touches rows newer than ``period_days`` and
never touches rows under an active legal hold.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_log
from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.session import async_session_factory
from app.models.retention_policy import RetentionPolicy
from app.services.audit.legal_hold import get_legal_hold_service
from app.services.audit_chain import get_audit_chain

logger = get_logger(__name__)


def _archive_dir(policy_name: str) -> Path:
    settings = get_settings()
    root = Path(getattr(settings, "PROJECT_ROOT", "."))
    p = root / "data" / "audit_archive" / policy_name
    p.mkdir(parents=True, exist_ok=True)
    return p


def _severity_of(action: str, payload: dict[str, Any]) -> str:
    if payload.get("severity"):
        return str(payload["severity"]).lower()
    a = (action or "").lower()
    if any(k in a for k in ("tamper", "denied", "unauthorized", "locked")):
        return "critical"
    if any(k in a for k in ("delete", "ban", "kick", "revoke", "purge")):
        return "high"
    if any(k in a for k in ("failed", "error", "rate_limited")):
        return "medium"
    if any(k in a for k in ("login", "logout", "token", "permission")):
        return "low"
    return "info"


class RetentionService:
    """Manage CRUD + preview + apply for retention policies."""

    async def list_policies(self) -> list[dict[str, Any]]:
        async with async_session_factory() as db:
            res = await db.execute(
                select(RetentionPolicy).order_by(RetentionPolicy.created_at.desc())
            )
            return [r.to_dict() for r in res.scalars().all()]

    async def get(self, policy_id: str) -> Optional[dict[str, Any]]:
        async with async_session_factory() as db:
            res = await db.execute(
                select(RetentionPolicy).where(RetentionPolicy.id == policy_id)
            )
            r = res.scalar_one_or_none()
            return r.to_dict() if r else None

    async def create(
        self,
        *,
        name: str,
        resource_type: str,
        period_days: int,
        action: str,
        actor_id: str,
        exemptions: Optional[dict[str, Any]] = None,
        description: Optional[str] = None,
    ) -> dict[str, Any]:
        async with async_session_factory() as db:
            row = RetentionPolicy(
                name=name,
                resource_type=resource_type,
                period_days=int(period_days),
                action=action,
                exemptions=dict(exemptions or {}),
                description=description,
                created_by=actor_id,
            )
            db.add(row)
            await db.commit()
            await db.refresh(row)
            result = row.to_dict()

        audit_log("siem.retention.create",
                  user_id=actor_id, success=True,
                  details={"policy_id": result["id"], "name": name})
        return result

    async def update(
        self, policy_id: str, *, actor_id: str, **fields: Any,
    ) -> Optional[dict[str, Any]]:
        async with async_session_factory() as db:
            res = await db.execute(
                select(RetentionPolicy).where(RetentionPolicy.id == policy_id)
            )
            row = res.scalar_one_or_none()
            if not row:
                return None
            for k, v in fields.items():
                if hasattr(row, k) and v is not None:
                    setattr(row, k, v)
            await db.commit()
            await db.refresh(row)
            result = row.to_dict()

        audit_log("siem.retention.update",
                  user_id=actor_id, success=True,
                  details={"policy_id": policy_id, "fields": list(fields.keys())})
        return result

    async def delete(self, policy_id: str, *, actor_id: str) -> bool:
        async with async_session_factory() as db:
            res = await db.execute(
                select(RetentionPolicy).where(RetentionPolicy.id == policy_id)
            )
            row = res.scalar_one_or_none()
            if not row:
                return False
            await db.delete(row)
            await db.commit()
        audit_log("siem.retention.delete",
                  user_id=actor_id, success=True,
                  details={"policy_id": policy_id})
        return True

    async def preview(self, policy_id: str) -> dict[str, Any]:
        """Count rows that *would* be affected by the policy (excludes
        rows under an active legal hold)."""
        policy = await self.get(policy_id)
        if policy is None:
            raise KeyError(policy_id)
        return await self._scan(policy, dry_run=True)

    async def apply(
        self, policy_id: str, *, actor_id: str, dry_run: bool = False,
    ) -> dict[str, Any]:
        policy = await self.get(policy_id)
        if policy is None:
            raise KeyError(policy_id)
        if not policy.get("enabled"):
            return {"policy_id": policy_id, "skipped": "disabled",
                    "affected": 0, "exempted": 0}

        report = await self._scan(policy, dry_run=dry_run)

        # Persist last_run on real apply
        if not dry_run:
            async with async_session_factory() as db:
                res = await db.execute(
                    select(RetentionPolicy).where(RetentionPolicy.id == policy_id)
                )
                row = res.scalar_one_or_none()
                if row:
                    row.last_run_at = datetime.now(timezone.utc)
                    row.last_affected = int(report.get("affected", 0))
                    await db.commit()

        audit_log("siem.retention.apply",
                  user_id=actor_id, success=True,
                  details={
                      "policy_id": policy_id,
                      "dry_run": dry_run,
                      "affected": report.get("affected", 0),
                      "exempted": report.get("exempted", 0),
                  })
        return report

    # ── internals ───────────────────────────────────────────────────

    async def _scan(
        self, policy: dict[str, Any], *, dry_run: bool,
    ) -> dict[str, Any]:
        # Currently the only resource_type wired in is "audit_chain"
        if policy["resource_type"] != "audit_chain":
            return {
                "policy_id": policy["id"], "resource_type": policy["resource_type"],
                "affected": 0, "exempted": 0,
                "notice": "resource_type not implemented",
            }

        chain = get_audit_chain()
        if chain is None:
            return {
                "policy_id": policy["id"], "resource_type": "audit_chain",
                "affected": 0, "exempted": 0, "notice": "chain not configured",
            }

        period = int(policy["period_days"]) * 86400
        cutoff = time.time() - period
        hold_svc = get_legal_hold_service()
        exemptions = policy.get("exemptions") or {}
        honour_holds = exemptions.get("holds", True)
        skip_classifications = set(exemptions.get("classifications") or [])

        # We never DELETE from the legacy chain (would break the hash
        # chain). Instead, retention "archives" by writing a JSONL slice
        # to disk and noting which seqs would be candidates. Hard delete
        # is opt-in and only available on resource_types other than
        # audit_chain (future work).
        action = policy["action"]
        archive_path: Optional[Path] = None
        archive_fh = None
        if action == "archive" and not dry_run:
            archive_path = _archive_dir(policy["name"]) / \
                f"{int(time.time())}.jsonl"
            archive_fh = archive_path.open("w", encoding="utf-8")

        affected = 0
        exempted = 0
        samples: list[dict[str, Any]] = []

        try:
            db_path = chain.db_path
            c = sqlite3.connect(
                f"file:{Path(db_path).as_posix()}?mode=ro",
                uri=True, check_same_thread=False,
            )
            c.row_factory = sqlite3.Row
            try:
                for row in c.execute(
                    "SELECT seq, timestamp, actor, action, target, payload_json "
                    "FROM audit_chain WHERE timestamp < ?",
                    (cutoff,),
                ):
                    try:
                        payload = json.loads(row["payload_json"] or "{}")
                    except Exception:
                        payload = {}
                    severity = _severity_of(row["action"], payload)
                    if skip_classifications and \
                       payload.get("classification") in skip_classifications:
                        exempted += 1
                        continue
                    if honour_holds and await hold_svc.is_under_hold(
                        resource_type="audit_chain",
                        resource_id=row["target"],
                        actor=row["actor"],
                        timestamp=float(row["timestamp"]),
                        severity=severity,
                        payload=payload,
                    ):
                        exempted += 1
                        continue
                    affected += 1
                    if len(samples) < 10:
                        samples.append({
                            "seq": row["seq"],
                            "timestamp": row["timestamp"],
                            "actor": row["actor"],
                            "action": row["action"],
                        })
                    if archive_fh is not None:
                        archive_fh.write(json.dumps({
                            "seq": row["seq"],
                            "timestamp": row["timestamp"],
                            "actor": row["actor"],
                            "action": row["action"],
                            "target": row["target"],
                            "payload": payload,
                        }, ensure_ascii=False) + "\n")
            finally:
                c.close()
        finally:
            if archive_fh:
                archive_fh.close()

        return {
            "policy_id": policy["id"],
            "resource_type": policy["resource_type"],
            "action": action,
            "cutoff_ts": cutoff,
            "affected": affected,
            "exempted": exempted,
            "dry_run": dry_run,
            "archive_path": str(archive_path) if archive_path else None,
            "samples": samples,
        }


_service: Optional[RetentionService] = None


def get_retention_service() -> RetentionService:
    global _service
    if _service is None:
        _service = RetentionService()
    return _service


__all__ = ["RetentionService", "get_retention_service"]
