"""
Phase 6 / Module AB — compliance report generation.

Produces three kinds of evidence packs:

* ``generate_soc2_report(period_start, period_end)``
* ``generate_gdpr_report()``
* ``generate_hipaa_log(period_start, period_end)``

Each function returns ``(jsonl_path, pdf_path_or_none)``.  PDFs are
produced via ``reportlab`` if installed; if not, only the JSONL output
is returned — the operator can still ship that to auditors.
"""
from __future__ import annotations

import io
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.session import async_session_factory
from app.models.compliance import (
    ConsentRecord,
    DataDeletionRequest,
    DataExportRequest,
    PIIInventoryEntry,
    RetentionPolicy,
)

logger = get_logger(__name__)


# ── optional dep ────────────────────────────────────────────────


try:                                                                 # pragma: no cover
    from reportlab.lib.pagesizes import LETTER                        # type: ignore
    from reportlab.lib.styles import getSampleStyleSheet              # type: ignore
    from reportlab.platypus import (                                  # type: ignore
        Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
    )
    from reportlab.lib import colors                                  # type: ignore
    _REPORTLAB_OK = True
except Exception:                                                    # pragma: no cover
    _REPORTLAB_OK = False


def reports_dir() -> Path:
    s = get_settings()
    root = Path(getattr(s, "PROJECT_ROOT", "."))
    p = root / "data" / "compliance" / "reports"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _write_jsonl(name: str, rows: List[Dict[str, Any]]) -> Path:
    path = reports_dir() / f"{name}.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, default=str) + "\n")
    return path


def _maybe_pdf(name: str, title: str, sections: List[Tuple[str, List[List[str]]]]) -> Optional[Path]:
    if not _REPORTLAB_OK:
        return None
    path = reports_dir() / f"{name}.pdf"
    doc = SimpleDocTemplate(str(path), pagesize=LETTER)
    styles = getSampleStyleSheet()
    story: list[Any] = [Paragraph(title, styles["Title"]), Spacer(1, 18)]
    for section_title, table_rows in sections:
        story.append(Paragraph(section_title, styles["Heading2"]))
        if table_rows:
            t = Table(table_rows, repeatRows=1)
            t.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#23303f")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
            ]))
            story.append(t)
        else:
            story.append(Paragraph("(none)", styles["BodyText"]))
        story.append(Spacer(1, 12))
    doc.build(story)
    return path


# ── SOC 2 ───────────────────────────────────────────────────────


async def generate_soc2_report(period_start: datetime, period_end: datetime) -> Dict[str, Any]:
    rid = uuid.uuid4().hex[:8]
    rows: List[Dict[str, Any]] = []
    async with async_session_factory() as db:
        # Access events from audit log
        try:
            from app.models.audit_log import AuditLog
            audits = (await db.execute(
                select(AuditLog).where(
                    AuditLog.created_at >= period_start,
                    AuditLog.created_at <= period_end,
                ).limit(20000)
            )).scalars().all()
            for a in audits:
                rows.append({
                    "ts": a.created_at.isoformat() if a.created_at else None,
                    "actor": getattr(a, "actor_id", None) or getattr(a, "user_id", None),
                    "action": getattr(a, "action", None) or getattr(a, "event", None),
                    "success": getattr(a, "success", None),
                })
        except Exception as e:
            logger.warning("soc2_audit_load_failed", error=str(e))

        # Retention policies
        rps = (await db.execute(select(RetentionPolicy))).scalars().all()
        # Data subject requests
        exports = (await db.execute(select(DataExportRequest))).scalars().all()
        deletions = (await db.execute(select(DataDeletionRequest))).scalars().all()

    jsonl = _write_jsonl(f"soc2_{rid}", rows)
    summary_rows = [
        ["control", "value"],
        ["period_start", period_start.isoformat()],
        ["period_end", period_end.isoformat()],
        ["audit_events", str(len(rows))],
        ["retention_policies", str(len(rps))],
        ["export_requests", str(len(exports))],
        ["deletion_requests", str(len(deletions))],
    ]
    pdf = _maybe_pdf(
        f"soc2_{rid}", "SOC 2 Type II Evidence Pack",
        [("Summary", summary_rows)],
    )
    return {
        "id": rid, "jsonl": str(jsonl),
        "pdf": str(pdf) if pdf else None,
        "events": len(rows),
    }


# ── GDPR ────────────────────────────────────────────────────────


async def generate_gdpr_report() -> Dict[str, Any]:
    rid = uuid.uuid4().hex[:8]
    async with async_session_factory() as db:
        inv = (await db.execute(select(PIIInventoryEntry))).scalars().all()
        exports = (await db.execute(select(DataExportRequest))).scalars().all()
        deletions = (await db.execute(select(DataDeletionRequest))).scalars().all()
        consents = (await db.execute(select(ConsentRecord).limit(5000))).scalars().all()
    rows = [
        {
            "table": r.table_name, "column": r.column_name,
            "classification": r.classification,
            "encryption": r.encryption_status,
            "masking": r.masking_rule,
        }
        for r in inv
    ]
    jsonl = _write_jsonl(f"gdpr_{rid}", rows)
    inv_rows = [["table", "column", "classification", "encryption"]] + [
        [r["table"], r["column"], r["classification"], r["encryption"]]
        for r in rows
    ]
    pdf = _maybe_pdf(
        f"gdpr_{rid}", "GDPR Data Inventory + Processing Register",
        [
            ("PII Inventory", inv_rows[:200]),
            ("Subject Requests", [
                ["kind", "count"],
                ["data exports", str(len(exports))],
                ["deletion requests", str(len(deletions))],
                ["consent records", str(len(consents))],
            ]),
        ],
    )
    return {"id": rid, "jsonl": str(jsonl),
            "pdf": str(pdf) if pdf else None, "columns": len(rows)}


# ── HIPAA ───────────────────────────────────────────────────────


async def generate_hipaa_log(period_start: datetime, period_end: datetime) -> Dict[str, Any]:
    rid = uuid.uuid4().hex[:8]
    rows: List[Dict[str, Any]] = []
    async with async_session_factory() as db:
        phi_cols = (await db.execute(
            select(PIIInventoryEntry).where(PIIInventoryEntry.classification == "phi")
        )).scalars().all()
        phi_tables = sorted({c.table_name for c in phi_cols})
        try:
            from app.models.audit_log import AuditLog
            audits = (await db.execute(
                select(AuditLog).where(
                    AuditLog.created_at >= period_start,
                    AuditLog.created_at <= period_end,
                ).limit(50000)
            )).scalars().all()
            for a in audits:
                detail = getattr(a, "details", None)
                detail_str = json.dumps(detail) if isinstance(detail, (dict, list)) else str(detail or "")
                if any(t in detail_str for t in phi_tables):
                    rows.append({
                        "ts": a.created_at.isoformat() if a.created_at else None,
                        "actor": getattr(a, "actor_id", None) or getattr(a, "user_id", None),
                        "action": getattr(a, "action", None) or getattr(a, "event", None),
                        "details": detail_str[:512],
                    })
        except Exception as e:
            logger.warning("hipaa_audit_load_failed", error=str(e))
    jsonl = _write_jsonl(f"hipaa_{rid}", rows)
    pdf = _maybe_pdf(
        f"hipaa_{rid}", "HIPAA PHI Access Log",
        [
            ("PHI tables", [["table"]] + [[t] for t in phi_tables]),
            ("Access events", [["ts", "actor", "action"]] +
             [[r["ts"], r["actor"], r["action"]] for r in rows[:500]]),
        ],
    )
    return {
        "id": rid, "jsonl": str(jsonl),
        "pdf": str(pdf) if pdf else None,
        "phi_tables": len(phi_tables), "events": len(rows),
    }
