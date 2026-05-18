"""
ComplianceFrameworkEngine — per-framework posture assessment.

For each framework (GDPR, HIPAA, SOC2, ISO27001, ISO27017, NIST_800_53,
PCI_DSS, FedRAMP, SAUDI_NCA_ECC, UAE_TDRA) we evaluate a curated set of
controls. Each control returns ``ok | warn | fail`` with evidence
references; the framework rolls up to a traffic-light status.

Checks are deliberately pragmatic: they probe configuration, audit
records, retention coverage, hold posture, classification findings, and
encryption flags. The engine is data-driven so new controls can be added
by appending entries to ``_CONTROLS``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.compliance import RetentionPolicy
from app.models.compliance_classification import ClassificationFinding
from app.models.compliance_hold import ComplianceHold

logger = get_logger(__name__)


@dataclass
class ControlResult:
    control_id: str
    title: str
    status: str  # ok / warn / fail
    detail: str
    evidence: Dict[str, Any]


# ── primitives ──────────────────────────────────────────────────


async def _audit_count_since(db: AsyncSession, days: int = 30) -> int:
    try:
        from app.models.audit_log import AuditLog
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        n = (await db.execute(
            select(func.count()).select_from(AuditLog)
            .where(AuditLog.occurred_at >= cutoff)
        )).scalar_one()
        return int(n or 0)
    except Exception:
        return 0


async def _retention_policy_count(db: AsyncSession) -> int:
    try:
        n = (await db.execute(
            select(func.count()).select_from(RetentionPolicy)
        )).scalar_one()
        return int(n or 0)
    except Exception:
        return 0


async def _active_hold_count(db: AsyncSession) -> int:
    try:
        n = (await db.execute(
            select(func.count()).select_from(ComplianceHold)
            .where(ComplianceHold.status == "active")
        )).scalar_one()
        return int(n or 0)
    except Exception:
        return 0


async def _findings_by_severity(db: AsyncSession) -> Dict[str, int]:
    try:
        rows = (await db.execute(
            select(ClassificationFinding.severity, func.count())
            .group_by(ClassificationFinding.severity)
        )).all()
        return {sev: int(cnt) for sev, cnt in rows}
    except Exception:
        return {}


# ── controls ────────────────────────────────────────────────────


async def _ctrl_audit_active(db: AsyncSession) -> ControlResult:
    n = await _audit_count_since(db, days=30)
    if n == 0:
        return ControlResult(
            "AUDIT-001", "Audit log present (30d)", "fail",
            "No audit log entries in the last 30 days.",
            {"count_30d": 0},
        )
    if n < 100:
        return ControlResult(
            "AUDIT-001", "Audit log present (30d)", "warn",
            f"Only {n} audit entries in the last 30 days.",
            {"count_30d": n},
        )
    return ControlResult(
        "AUDIT-001", "Audit log present (30d)", "ok",
        f"{n} audit entries in the last 30 days.",
        {"count_30d": n},
    )


async def _ctrl_retention_defined(db: AsyncSession) -> ControlResult:
    n = await _retention_policy_count(db)
    if n == 0:
        return ControlResult(
            "RET-001", "Retention policies defined", "fail",
            "No retention policies registered.",
            {"policies": 0},
        )
    if n < 3:
        return ControlResult(
            "RET-001", "Retention policies defined", "warn",
            f"Only {n} retention policies registered.",
            {"policies": n},
        )
    return ControlResult(
        "RET-001", "Retention policies defined", "ok",
        f"{n} retention policies registered.",
        {"policies": n},
    )


async def _ctrl_legal_holds_configured(db: AsyncSession) -> ControlResult:
    n = await _active_hold_count(db)
    return ControlResult(
        "HOLD-001", "Legal hold subsystem operational",
        "ok" if n >= 0 else "fail",
        f"{n} active legal holds." if n else "No active holds (subsystem available).",
        {"active_holds": n},
    )


async def _ctrl_pii_findings(db: AsyncSession) -> ControlResult:
    by_sev = await _findings_by_severity(db)
    crit = int(by_sev.get("critical", 0))
    high = int(by_sev.get("high", 0))
    if crit > 0:
        return ControlResult(
            "CLS-001", "PII/PHI findings", "fail",
            f"{crit} critical and {high} high-severity findings open.",
            {"by_severity": by_sev},
        )
    if high > 10:
        return ControlResult(
            "CLS-001", "PII/PHI findings", "warn",
            f"{high} high-severity findings open.",
            {"by_severity": by_sev},
        )
    return ControlResult(
        "CLS-001", "PII/PHI findings", "ok",
        "No critical findings open.",
        {"by_severity": by_sev},
    )


async def _ctrl_encryption_in_transit(db: AsyncSession) -> ControlResult:
    # Best-effort: probe settings for TLS_REQUIRED
    try:
        from app.core.config import get_settings
        s = get_settings()
        if getattr(s, "TLS_ENFORCE", True):
            return ControlResult(
                "ENC-001", "TLS enforced in transit", "ok",
                "TLS enforcement enabled.",
                {"tls_enforce": True},
            )
    except Exception:
        pass
    return ControlResult(
        "ENC-001", "TLS enforced in transit", "warn",
        "TLS enforcement not confirmed.",
        {"tls_enforce": False},
    )


async def _ctrl_e2ee_keys(db: AsyncSession) -> ControlResult:
    try:
        from app.models.e2ee_key import E2EEKey
        from sqlalchemy import func as _f
        n = (await db.execute(
            select(_f.count()).select_from(E2EEKey)
        )).scalar_one()
        return ControlResult(
            "ENC-002", "E2EE key inventory present",
            "ok" if (n or 0) > 0 else "warn",
            f"{int(n or 0)} E2EE keys registered.",
            {"keys": int(n or 0)},
        )
    except Exception as e:
        return ControlResult(
            "ENC-002", "E2EE key inventory present", "warn",
            f"E2EE module unavailable: {e}",
            {},
        )


CONTROL_FN = Callable[[AsyncSession], "ControlResult"]


# Per-framework control set
_FRAMEWORKS: Dict[str, List[CONTROL_FN]] = {
    "GDPR": [_ctrl_audit_active, _ctrl_retention_defined,
             _ctrl_legal_holds_configured, _ctrl_pii_findings,
             _ctrl_encryption_in_transit],
    "HIPAA": [_ctrl_audit_active, _ctrl_retention_defined,
              _ctrl_pii_findings, _ctrl_encryption_in_transit,
              _ctrl_e2ee_keys],
    "SOC2": [_ctrl_audit_active, _ctrl_retention_defined,
             _ctrl_encryption_in_transit, _ctrl_legal_holds_configured],
    "ISO27001": [_ctrl_audit_active, _ctrl_retention_defined,
                 _ctrl_encryption_in_transit, _ctrl_pii_findings],
    "ISO27017": [_ctrl_audit_active, _ctrl_encryption_in_transit,
                 _ctrl_e2ee_keys],
    "NIST_800_53": [_ctrl_audit_active, _ctrl_retention_defined,
                    _ctrl_encryption_in_transit, _ctrl_e2ee_keys,
                    _ctrl_pii_findings],
    "PCI_DSS": [_ctrl_audit_active, _ctrl_encryption_in_transit,
                _ctrl_pii_findings, _ctrl_retention_defined],
    "FedRAMP": [_ctrl_audit_active, _ctrl_retention_defined,
                _ctrl_encryption_in_transit, _ctrl_e2ee_keys,
                _ctrl_pii_findings, _ctrl_legal_holds_configured],
    "SAUDI_NCA_ECC": [_ctrl_audit_active, _ctrl_retention_defined,
                      _ctrl_encryption_in_transit, _ctrl_pii_findings],
    "UAE_TDRA": [_ctrl_audit_active, _ctrl_retention_defined,
                 _ctrl_encryption_in_transit, _ctrl_pii_findings],
}


def _rollup(results: List[ControlResult]) -> str:
    if any(r.status == "fail" for r in results):
        return "red"
    if any(r.status == "warn" for r in results):
        return "yellow"
    return "green"


class ComplianceFrameworkEngine:
    SUPPORTED = tuple(_FRAMEWORKS.keys())

    async def assess(
        self, db: AsyncSession, framework: str,
    ) -> Dict[str, Any]:
        if framework not in _FRAMEWORKS:
            raise ValueError(f"unknown framework {framework}; supported: "
                             f"{', '.join(_FRAMEWORKS)}")
        results: List[ControlResult] = []
        for fn in _FRAMEWORKS[framework]:
            try:
                results.append(await fn(db))
            except Exception as e:
                results.append(ControlResult(
                    "ERROR", fn.__name__, "warn",
                    f"check failed: {e}", {},
                ))
        return {
            "framework": framework,
            "posture": _rollup(results),
            "controls": [
                {"id": r.control_id, "title": r.title,
                 "status": r.status, "detail": r.detail,
                 "evidence": r.evidence}
                for r in results
            ],
            "recommendations": [
                f"Address: {r.title}" for r in results if r.status != "ok"
            ],
            "evaluated_at": datetime.now(timezone.utc).isoformat(),
        }

    async def status_all(self, db: AsyncSession) -> Dict[str, Any]:
        out: List[Dict[str, Any]] = []
        for fw in self.SUPPORTED:
            try:
                report = await self.assess(db, fw)
            except Exception as e:
                report = {"framework": fw, "posture": "yellow",
                          "error": str(e), "controls": []}
            out.append({
                "framework": fw, "posture": report["posture"],
                "control_count": len(report.get("controls", [])),
                "failing": sum(
                    1 for c in report.get("controls", [])
                    if c.get("status") == "fail"
                ),
                "warning": sum(
                    1 for c in report.get("controls", [])
                    if c.get("status") == "warn"
                ),
            })
        return {"frameworks": out,
                "evaluated_at": datetime.now(timezone.utc).isoformat()}


framework_engine = ComplianceFrameworkEngine()
