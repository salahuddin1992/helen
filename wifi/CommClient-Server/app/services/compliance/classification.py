"""
DataClassificationService — content-based PII/PHI/financial detection.

* Rule store with CRUD (regex / keyword / file-type / luhn).
* Built-in patterns: credit card (Luhn-validated), SSN, IBAN, passport,
  international phone, email, IPv4/IPv6, GPS coords, medical record #.
* ``scan(scope, dry_run)`` iterates rows, runs every active rule, and
  records confidence-scored findings.
* Bulk reclassify.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_log
from app.core.logging import get_logger
from app.db.session import async_session_factory
from app.models.compliance_classification import (
    VALID_RULE_ACTIONS,
    VALID_RULE_KINDS,
    VALID_RULE_SEVERITIES,
    ClassificationFinding,
    ClassificationRule,
)

logger = get_logger(__name__)


# ── built-in patterns ─────────────────────────────────────────


BUILTIN_PATTERNS: List[Dict[str, Any]] = [
    {"name": "credit_card", "kind": "luhn",
     "pattern": r"\b(?:\d[ -]?){13,19}\b",
     "action": "alert", "severity": "high", "classification": "financial"},
    {"name": "ssn_us", "kind": "regex",
     "pattern": r"\b\d{3}-\d{2}-\d{4}\b",
     "action": "alert", "severity": "critical", "classification": "pii"},
    {"name": "iban", "kind": "regex",
     "pattern": r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b",
     "action": "tag", "severity": "high", "classification": "financial"},
    {"name": "passport", "kind": "regex",
     "pattern": r"\b[A-Z][0-9]{7,9}\b",
     "action": "tag", "severity": "high", "classification": "pii"},
    {"name": "phone_intl", "kind": "regex",
     "pattern": r"\+\d{1,3}[\s.-]?\(?\d{1,4}\)?[\s.-]?\d{3,4}[\s.-]?\d{3,4}",
     "action": "tag", "severity": "low", "classification": "pii"},
    {"name": "email", "kind": "regex",
     "pattern": r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b",
     "action": "tag", "severity": "low", "classification": "pii"},
    {"name": "ipv4", "kind": "regex",
     "pattern": r"\b(?:\d{1,3}\.){3}\d{1,3}\b",
     "action": "tag", "severity": "info", "classification": "pii"},
    {"name": "ipv6", "kind": "regex",
     "pattern": r"\b(?:[0-9A-Fa-f]{1,4}:){2,7}[0-9A-Fa-f]{1,4}\b",
     "action": "tag", "severity": "info", "classification": "pii"},
    {"name": "gps_coords", "kind": "regex",
     "pattern": r"-?\d{1,3}\.\d{3,6}\s*,\s*-?\d{1,3}\.\d{3,6}",
     "action": "tag", "severity": "medium", "classification": "pii"},
    {"name": "medical_record_number", "kind": "regex",
     "pattern": r"\bMRN[:\s-]?\d{6,12}\b",
     "action": "alert", "severity": "high", "classification": "phi"},
]


def _luhn_valid(card: str) -> bool:
    digits = [int(c) for c in re.sub(r"\D", "", card)]
    if len(digits) < 13 or len(digits) > 19:
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


class DataClassificationService:
    async def list_rules(self, db: AsyncSession) -> List[ClassificationRule]:
        return list((await db.execute(
            select(ClassificationRule).order_by(desc(ClassificationRule.created_at))
        )).scalars().all())

    async def create_rule(
        self, db: AsyncSession, *,
        name: str, kind: str, pattern: str,
        action: str = "tag", severity: str = "medium",
        classification: str = "pii",
        description: Optional[str] = None,
        enabled: bool = True, is_builtin: bool = False,
        actor_id: str = "system",
    ) -> ClassificationRule:
        if kind not in VALID_RULE_KINDS:
            raise ValueError(f"kind must be one of {VALID_RULE_KINDS}")
        if action not in VALID_RULE_ACTIONS:
            raise ValueError(f"action must be one of {VALID_RULE_ACTIONS}")
        if severity not in VALID_RULE_SEVERITIES:
            raise ValueError(f"severity must be one of {VALID_RULE_SEVERITIES}")
        # Validate regex compiles
        if kind in ("regex", "luhn"):
            re.compile(pattern)
        rule = ClassificationRule(
            id=uuid.uuid4().hex,
            name=name, description=description,
            kind=kind, pattern=pattern,
            action=action, severity=severity,
            classification=classification,
            enabled=enabled, is_builtin=is_builtin,
        )
        db.add(rule)
        await db.commit()
        audit_log("compliance.classification_rule_created",
                  user_id=actor_id, success=True,
                  details={"rule_id": rule.id, "name": name})
        return rule

    async def update_rule(
        self, db: AsyncSession, rule_id: str, *,
        patch: Dict[str, Any], actor_id: str,
    ) -> ClassificationRule:
        rule = (await db.execute(
            select(ClassificationRule).where(ClassificationRule.id == rule_id)
        )).scalar_one_or_none()
        if rule is None:
            raise LookupError(rule_id)
        for k, v in patch.items():
            if hasattr(rule, k) and v is not None:
                setattr(rule, k, v)
        await db.commit()
        audit_log("compliance.classification_rule_updated",
                  user_id=actor_id, success=True,
                  details={"rule_id": rule_id})
        return rule

    async def delete_rule(
        self, db: AsyncSession, rule_id: str, *, actor_id: str,
    ) -> None:
        rule = (await db.execute(
            select(ClassificationRule).where(ClassificationRule.id == rule_id)
        )).scalar_one_or_none()
        if rule is None:
            raise LookupError(rule_id)
        await db.delete(rule)
        await db.commit()
        audit_log("compliance.classification_rule_deleted",
                  user_id=actor_id, success=True,
                  details={"rule_id": rule_id})

    async def bootstrap_builtins(self, db: AsyncSession) -> int:
        """Insert any built-in rules that don't yet exist."""
        existing = {
            r.name for r in (await db.execute(
                select(ClassificationRule).where(ClassificationRule.is_builtin.is_(True))
            )).scalars().all()
        }
        added = 0
        for p in BUILTIN_PATTERNS:
            if p["name"] in existing:
                continue
            db.add(ClassificationRule(
                id=uuid.uuid4().hex,
                name=p["name"], kind=p["kind"], pattern=p["pattern"],
                action=p["action"], severity=p["severity"],
                classification=p["classification"], enabled=True,
                is_builtin=True,
            ))
            added += 1
        if added:
            await db.commit()
        return added

    async def list_findings(
        self, db: AsyncSession, *,
        severity: Optional[str] = None,
        resource_type: Optional[str] = None,
        limit: int = 200, offset: int = 0,
    ) -> List[ClassificationFinding]:
        q = select(ClassificationFinding)
        if severity:
            q = q.where(ClassificationFinding.severity == severity)
        if resource_type:
            q = q.where(ClassificationFinding.resource_type == resource_type)
        q = q.order_by(desc(ClassificationFinding.found_at)).offset(offset).limit(limit)
        return list((await db.execute(q)).scalars().all())

    async def scan(
        self, db: AsyncSession, *,
        scope: Optional[Dict[str, Any]] = None,
        dry_run: bool = False, actor_id: str = "system",
    ) -> Dict[str, Any]:
        scope = scope or {}
        sources: List[str] = scope.get("sources") or ["messages", "files"]
        limit_per = int(scope.get("limit_per_source") or 1000)

        rules = [
            r for r in await self.list_rules(db)
            if r.enabled
        ]
        if not rules:
            # Auto-bootstrap on empty
            await self.bootstrap_builtins(db)
            rules = [r for r in await self.list_rules(db) if r.enabled]

        compiled: List[Tuple[ClassificationRule, Any]] = []
        for r in rules:
            try:
                if r.kind in ("regex", "luhn"):
                    compiled.append((r, re.compile(r.pattern)))
                elif r.kind == "keyword":
                    compiled.append((r, r.pattern.lower()))
                elif r.kind == "file_type":
                    compiled.append((r, r.pattern.lstrip(".").lower()))
            except re.error as e:
                logger.warning("rule_compile_failed", rule_id=r.id, error=str(e))

        findings: List[Dict[str, Any]] = []
        scanned = 0
        if "messages" in sources:
            try:
                from app.models.message import Message
                rows = (await db.execute(
                    select(Message).limit(limit_per)
                )).scalars().all()
                for m in rows:
                    scanned += 1
                    text = getattr(m, "content", None) or ""
                    if not text:
                        continue
                    for rule, comp in compiled:
                        f = self._evaluate(rule, comp, text, "messages", str(m.id), "content")
                        if f:
                            findings.append(f)
            except Exception as e:
                logger.warning("classification_scan_messages_failed", error=str(e))

        if "files" in sources:
            try:
                from app.models.file import FileRecord
                rows = (await db.execute(
                    select(FileRecord).limit(limit_per)
                )).scalars().all()
                for fr in rows:
                    scanned += 1
                    fn = getattr(fr, "filename", "") or ""
                    mime = getattr(fr, "mime_type", "") or ""
                    blob = f"{fn} {mime}"
                    for rule, comp in compiled:
                        if rule.kind == "file_type":
                            ext = fn.rsplit(".", 1)[-1].lower() if "." in fn else ""
                            if ext == comp:
                                findings.append(self._mk_finding(
                                    rule, "files", str(fr.id), "filename",
                                    matched=fn, confidence=95,
                                ))
                        else:
                            f = self._evaluate(rule, comp, blob, "files", str(fr.id), "filename")
                            if f:
                                findings.append(f)
            except Exception as e:
                logger.warning("classification_scan_files_failed", error=str(e))

        if not dry_run and findings:
            for f in findings:
                db.add(ClassificationFinding(
                    id=uuid.uuid4().hex,
                    rule_id=f["rule_id"],
                    resource_type=f["resource_type"],
                    resource_id=f["resource_id"],
                    field=f.get("field"),
                    severity=f["severity"],
                    confidence=f["confidence"],
                    evidence=f.get("evidence"),
                    matched_text=f.get("matched_text"),
                    extras=f.get("extras"),
                ))
            await db.commit()

        report = {
            "job_id": uuid.uuid4().hex,
            "scanned": scanned,
            "findings": len(findings),
            "dry_run": dry_run,
            "by_severity": _count_by(findings, "severity"),
            "by_rule": _count_by(findings, "rule_name"),
        }
        audit_log("compliance.classification_scan", user_id=actor_id, success=True,
                  details=report)
        return report

    # ── helpers ────────────────────────────────────────────

    def _evaluate(
        self, rule: ClassificationRule, comp: Any,
        text: str, resource_type: str, resource_id: str, field: str,
    ) -> Optional[Dict[str, Any]]:
        if rule.kind == "regex":
            m = comp.search(text)
            if not m:
                return None
            return self._mk_finding(
                rule, resource_type, resource_id, field,
                matched=m.group(0), confidence=85,
            )
        if rule.kind == "luhn":
            for m in comp.finditer(text):
                candidate = m.group(0)
                if _luhn_valid(candidate):
                    return self._mk_finding(
                        rule, resource_type, resource_id, field,
                        matched=candidate, confidence=99,
                        extras={"luhn_validated": True},
                    )
            return None
        if rule.kind == "keyword":
            if comp in (text or "").lower():
                return self._mk_finding(
                    rule, resource_type, resource_id, field,
                    matched=comp, confidence=70,
                )
            return None
        return None

    def _mk_finding(
        self, rule: ClassificationRule,
        resource_type: str, resource_id: str, field: str,
        *, matched: str, confidence: int,
        extras: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return {
            "rule_id": rule.id,
            "rule_name": rule.name,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "field": field,
            "severity": rule.severity,
            "confidence": int(confidence),
            "matched_text": (matched or "")[:200],
            "evidence": f"matched '{rule.name}' on {field}",
            "extras": extras,
        }


def _count_by(items: Iterable[Dict[str, Any]], key: str) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for it in items:
        v = it.get(key) or "—"
        out[str(v)] = out.get(str(v), 0) + 1
    return out


classification_service = DataClassificationService()
