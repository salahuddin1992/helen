"""
ComplianceReportGenerator — per-framework reports in JSON / CSV / PDF.
"""
from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_log
from app.core.logging import get_logger
from app.models.compliance_report import (
    VALID_FRAMEWORKS,
    VALID_REPORT_FORMATS,
    ComplianceReport,
)
from app.services.compliance.framework_engine import framework_engine

logger = get_logger(__name__)


def _reports_root() -> Path:
    try:
        from app.core.config import get_settings
        s = get_settings()
        root = Path(getattr(s, "PROJECT_ROOT", "."))
    except Exception:
        root = Path(".")
    p = root / "data" / "compliance" / "reports"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _signing_key() -> bytes:
    try:
        from app.core.config import get_settings
        s = get_settings()
        key = (
            getattr(s, "COMPLIANCE_SIGNING_KEY", None)
            or getattr(s, "SECRET_KEY", None)
            or "compliance-default-signing-key"
        )
    except Exception:
        key = os.environ.get("COMPLIANCE_SIGNING_KEY", "compliance-default-signing-key")
    if isinstance(key, str):
        key = key.encode("utf-8")
    return key


def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


class ComplianceReportGenerator:
    async def generate(
        self,
        db: AsyncSession,
        *,
        framework: str,
        format: str = "json",
        period_days: int = 90,
        signed: bool = True,
        actor_id: str = "system",
    ) -> ComplianceReport:
        if framework not in VALID_FRAMEWORKS:
            raise ValueError(f"framework must be one of {VALID_FRAMEWORKS}")
        if format not in VALID_REPORT_FORMATS:
            raise ValueError(f"format must be one of {VALID_REPORT_FORMATS}")

        report_id = uuid.uuid4().hex
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=max(1, period_days))
        row = ComplianceReport(
            id=report_id, framework=framework, format=format,
            period_start=start, period_end=end, status="running",
            signed=signed, created_by=actor_id,
        )
        db.add(row)
        await db.flush()

        try:
            assessment = await framework_engine.assess(db, framework)
            payload = {
                "report_id": report_id,
                "framework": framework,
                "period_start": start.isoformat(),
                "period_end": end.isoformat(),
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "assessment": assessment,
            }
            ext = {"json": "json", "csv": "csv", "pdf": "pdf"}[format]
            out_path = _reports_root() / f"{framework.lower()}_{report_id[:8]}.{ext}"

            if format == "json":
                out_path.write_bytes(json.dumps(payload, indent=2, default=str).encode())
            elif format == "csv":
                buf = io.StringIO()
                w = csv.writer(buf)
                w.writerow(["control_id", "title", "status", "detail"])
                for c in assessment.get("controls", []):
                    w.writerow([c.get("id"), c.get("title"),
                                c.get("status"), c.get("detail")])
                out_path.write_text(buf.getvalue(), encoding="utf-8")
            elif format == "pdf":
                self._write_pdf(out_path, payload)

            sig = None
            if signed:
                sig = hmac.new(
                    _signing_key(), out_path.read_bytes(), hashlib.sha256,
                ).hexdigest()

            row.status = "ready"
            row.file_path = str(out_path)
            row.sha256 = _sha256_file(out_path)
            row.signature = sig
            row.size_bytes = out_path.stat().st_size
            row.summary = {
                "posture": assessment.get("posture"),
                "control_count": len(assessment.get("controls", [])),
            }
            await db.commit()
            audit_log("compliance.report_generated_v2", user_id=actor_id, success=True,
                      details={"report_id": report_id, "framework": framework,
                               "posture": assessment.get("posture")})
            return row
        except Exception as e:
            row.status = "failed"
            row.error_message = str(e)[:1024]
            await db.commit()
            raise

    def _write_pdf(self, path: Path, payload: Dict[str, Any]) -> None:
        try:
            from reportlab.lib.pagesizes import LETTER
            from reportlab.platypus import (
                Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
            )
            from reportlab.lib.styles import getSampleStyleSheet
            from reportlab.lib import colors

            styles = getSampleStyleSheet()
            doc = SimpleDocTemplate(str(path), pagesize=LETTER)
            story = []
            story.append(Paragraph(
                f"<b>Compliance Report — {payload['framework']}</b>",
                styles["Title"]))
            story.append(Paragraph(
                f"Period: {payload['period_start']} – {payload['period_end']}",
                styles["Normal"]))
            story.append(Paragraph(
                f"Posture: <b>{payload['assessment']['posture'].upper()}</b>",
                styles["Heading2"]))
            story.append(Spacer(1, 10))
            rows = [["Control", "Title", "Status", "Detail"]]
            for c in payload["assessment"].get("controls", []):
                rows.append([c.get("id"), c.get("title"),
                             c.get("status"), (c.get("detail") or "")[:80]])
            t = Table(rows, hAlign="LEFT")
            t.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
            ]))
            story.append(t)
            doc.build(story)
        except Exception:
            # Plain-text fallback
            path.write_text(json.dumps(payload, indent=2, default=str),
                            encoding="utf-8")

    async def drift(
        self, db: AsyncSession, report_id_a: str, report_id_b: str,
    ) -> Dict[str, Any]:
        """Compare two reports and return added/removed/changed controls."""
        ra = (await db.execute(
            select(ComplianceReport).where(ComplianceReport.id == report_id_a)
        )).scalar_one_or_none()
        rb = (await db.execute(
            select(ComplianceReport).where(ComplianceReport.id == report_id_b)
        )).scalar_one_or_none()
        if not ra or not rb:
            raise LookupError("one or both reports not found")
        a = self._load_payload(ra)
        b = self._load_payload(rb)
        ca = {c["id"]: c for c in a.get("assessment", {}).get("controls", [])}
        cb = {c["id"]: c for c in b.get("assessment", {}).get("controls", [])}
        added = [cb[k] for k in cb.keys() - ca.keys()]
        removed = [ca[k] for k in ca.keys() - cb.keys()]
        changed = []
        for k in ca.keys() & cb.keys():
            if ca[k].get("status") != cb[k].get("status"):
                changed.append({"control": k, "from": ca[k]["status"],
                                "to": cb[k]["status"]})
        return {"report_a": report_id_a, "report_b": report_id_b,
                "added": added, "removed": removed, "changed": changed}

    def _load_payload(self, row: ComplianceReport) -> Dict[str, Any]:
        if not row.file_path:
            return {}
        p = Path(row.file_path)
        if not p.exists():
            return {}
        try:
            if row.format == "json":
                return json.loads(p.read_bytes())
        except Exception:
            pass
        return {}

    async def list_reports(
        self, db: AsyncSession, *,
        framework: Optional[str] = None,
        limit: int = 100, offset: int = 0,
    ) -> List[ComplianceReport]:
        q = select(ComplianceReport)
        if framework:
            q = q.where(ComplianceReport.framework == framework)
        from sqlalchemy import desc
        q = q.order_by(desc(ComplianceReport.created_at)).offset(offset).limit(limit)
        return list((await db.execute(q)).scalars().all())


report_generator = ComplianceReportGenerator()
