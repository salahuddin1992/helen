"""
DR v2 ReportGenerator — framework-aware compliance reports.

Frameworks
----------
* ISO 22301 (Business Continuity Management Systems)
* SOC 2 CC9 (Risk Mitigation)
* HIPAA §164.308(a)(7) (Contingency Plan)
* NIST SP 800-34 (Contingency Planning Guide for Federal Information Systems)

Formats
-------
* ``json``  — structured report
* ``csv``   — flattened evidence rows
* ``pdf``   — best-effort: uses ``reportlab`` if available, else returns
              a plaintext-rendered PDF surrogate (a ``.txt`` blob with a
              ``application/pdf`` content-type is NOT acceptable — we
              return a ``text/plain`` content-type when ``reportlab``
              is missing and let the caller decide what to do).
"""
from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.session import async_session_factory
from app.models.dr_v2 import (
    DRBackup,
    DRDrillV2,
    DRJob,
    DRPolicy,
)
from app.services.dr.rpo_rto_meter import measure as measure_rpo_rto


logger = get_logger(__name__)


VALID_FRAMEWORKS = ("iso-22301", "soc2-cc9", "hipaa-164.308a7", "nist-800-34")
VALID_FORMATS = ("json", "csv", "pdf")


try:  # pragma: no cover — optional
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    )
    from reportlab.lib import colors
    _REPORTLAB_OK = True
except Exception:
    _REPORTLAB_OK = False


_FRAMEWORK_TITLES = {
    "iso-22301": "ISO 22301 — Business Continuity Management System Evidence",
    "soc2-cc9": "SOC 2 CC9 — Risk Mitigation & Recovery Evidence",
    "hipaa-164.308a7": "HIPAA §164.308(a)(7) — Contingency Plan Evidence",
    "nist-800-34": "NIST SP 800-34 — Contingency Planning Evidence",
}


_FRAMEWORK_CONTROLS = {
    "iso-22301": [
        ("8.4.4", "Recovery — restore procedures documented and tested"),
        ("8.4.5", "Recovery — minimum acceptable recovery time"),
        ("9.1.2", "Business continuity exercise programme"),
        ("9.3", "Management review of BCMS"),
    ],
    "soc2-cc9": [
        ("CC9.1", "Identify, select, and develop risk mitigation activities"),
        ("CC9.2", "Manage vendor risks"),
        ("A1.2", "Environmental protection / backup recovery"),
        ("A1.3", "Backups recovered + retention"),
    ],
    "hipaa-164.308a7": [
        ("(a)(7)(i)", "Contingency plan established"),
        ("(a)(7)(ii)(A)", "Data backup plan"),
        ("(a)(7)(ii)(B)", "Disaster recovery plan"),
        ("(a)(7)(ii)(C)", "Emergency mode operation plan"),
        ("(a)(7)(ii)(D)", "Testing and revision procedures"),
        ("(a)(7)(ii)(E)", "Applications & data criticality analysis"),
    ],
    "nist-800-34": [
        ("3.4.1", "Activation and notification phase"),
        ("3.4.2", "Recovery phase"),
        ("3.4.3", "Reconstitution phase"),
        ("3.5", "Plan testing, training, and exercises"),
        ("3.6", "Plan maintenance"),
    ],
}


def _parse_period(period: str) -> Tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    period = (period or "").strip().lower()
    if period in ("", "7d", "week"):
        return now - timedelta(days=7), now
    if period in ("30d", "month"):
        return now - timedelta(days=30), now
    if period in ("90d", "quarter"):
        return now - timedelta(days=90), now
    if period in ("365d", "year"):
        return now - timedelta(days=365), now
    # accept "YYYY-MM-DD..YYYY-MM-DD"
    if ".." in period:
        a, b = period.split("..", 1)
        try:
            return (datetime.fromisoformat(a).replace(tzinfo=timezone.utc),
                    datetime.fromisoformat(b).replace(tzinfo=timezone.utc))
        except Exception:
            pass
    return now - timedelta(days=30), now


class DRReportGenerator:
    async def generate(
        self,
        framework: str,
        period: str = "30d",
        fmt: str = "json",
    ) -> Tuple[bytes, str]:
        if framework not in VALID_FRAMEWORKS:
            raise ValueError(f"unknown framework: {framework}")
        if fmt not in VALID_FORMATS:
            raise ValueError(f"unknown format: {fmt}")

        start, end = _parse_period(period)
        evidence = await self._collect_evidence(start, end)
        report = {
            "framework": framework,
            "title": _FRAMEWORK_TITLES[framework],
            "controls": _FRAMEWORK_CONTROLS[framework],
            "period": {"from": start.isoformat(), "to": end.isoformat()},
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "evidence": evidence,
        }

        if fmt == "json":
            return json.dumps(report, indent=2, default=str).encode("utf-8"), \
                   "application/json"
        if fmt == "csv":
            return self._csv(report).encode("utf-8"), "text/csv"
        return self._pdf(report)

    async def _collect_evidence(
        self, start: datetime, end: datetime,
    ) -> Dict[str, Any]:
        async with async_session_factory() as db:
            backups = (await db.execute(
                select(DRBackup).where(
                    DRBackup.started_at >= start, DRBackup.started_at <= end,
                ).order_by(desc(DRBackup.started_at))
            )).scalars().all()
            drills = (await db.execute(
                select(DRDrillV2).where(
                    DRDrillV2.scheduled_at >= start,
                    DRDrillV2.scheduled_at <= end,
                ).order_by(desc(DRDrillV2.scheduled_at))
            )).scalars().all()
            policies = (await db.execute(
                select(DRPolicy)
            )).scalars().all()
            restore_jobs = (await db.execute(
                select(DRJob).where(
                    DRJob.kind == "restore",
                    DRJob.created_at >= start, DRJob.created_at <= end,
                ).order_by(desc(DRJob.created_at))
            )).scalars().all()
        meter = await measure_rpo_rto()
        return {
            "backups_count": len(backups),
            "backups_succeeded": sum(1 for b in backups if b.status == "succeeded"),
            "backups_failed": sum(1 for b in backups if b.status == "failed"),
            "drills_count": len(drills),
            "drills_succeeded": sum(1 for d in drills if d.status == "succeeded"),
            "policies_active": sum(1 for p in policies if p.enabled),
            "restore_attempts": len(restore_jobs),
            "rpo_rto": meter,
            "backups": [
                {"id": b.id, "started_at": b.started_at.isoformat() if b.started_at else None,
                 "size_bytes": b.size_bytes, "status": b.status,
                 "sha256_root": b.sha256_root,
                 "verified_ok": b.last_verify_ok}
                for b in backups
            ],
            "drills": [
                {"id": d.id, "scheduled_at": d.scheduled_at.isoformat() if d.scheduled_at else None,
                 "status": d.status, "rto_seconds": d.rto_seconds,
                 "rpo_seconds": d.rpo_seconds,
                 "integrity_ok": d.integrity_ok}
                for d in drills
            ],
        }

    def _csv(self, report: Dict[str, Any]) -> str:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["framework", report["framework"]])
        w.writerow(["title", report["title"]])
        w.writerow(["period_from", report["period"]["from"]])
        w.writerow(["period_to", report["period"]["to"]])
        w.writerow([])
        w.writerow(["control_id", "description"])
        for cid, desc_ in report["controls"]:
            w.writerow([cid, desc_])
        w.writerow([])
        w.writerow(["backup_id", "started_at", "size_bytes", "status",
                    "sha256_root", "verified_ok"])
        for b in report["evidence"]["backups"]:
            w.writerow([b["id"], b["started_at"], b["size_bytes"],
                        b["status"], b["sha256_root"], b["verified_ok"]])
        w.writerow([])
        w.writerow(["drill_id", "scheduled_at", "status", "rto_seconds",
                    "rpo_seconds", "integrity_ok"])
        for d in report["evidence"]["drills"]:
            w.writerow([d["id"], d["scheduled_at"], d["status"],
                        d["rto_seconds"], d["rpo_seconds"], d["integrity_ok"]])
        return buf.getvalue()

    def _pdf(self, report: Dict[str, Any]) -> Tuple[bytes, str]:
        if not _REPORTLAB_OK:
            # Plain-text fallback — caller knows by content-type.
            return self._csv(report).encode("utf-8"), "text/plain"
        buf = io.BytesIO()
        doc = SimpleDocTemplate(buf, pagesize=letter)
        styles = getSampleStyleSheet()
        story: List[Any] = []
        story.append(Paragraph(report["title"], styles["Title"]))
        story.append(Spacer(1, 12))
        story.append(Paragraph(
            f"Period: {report['period']['from']} — {report['period']['to']}",
            styles["Normal"],
        ))
        story.append(Paragraph(
            f"Generated at: {report['generated_at']}",
            styles["Normal"],
        ))
        story.append(Spacer(1, 12))
        story.append(Paragraph("Applicable Controls", styles["Heading2"]))
        ctrl_data = [["Control ID", "Description"]] + list(report["controls"])
        t = Table(ctrl_data, colWidths=[120, 380])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
        ]))
        story.append(t)
        story.append(Spacer(1, 12))
        ev = report["evidence"]
        summary = [
            ["Backups total", ev["backups_count"]],
            ["Backups succeeded", ev["backups_succeeded"]],
            ["Backups failed", ev["backups_failed"]],
            ["Drills total", ev["drills_count"]],
            ["Drills succeeded", ev["drills_succeeded"]],
            ["Policies active", ev["policies_active"]],
            ["Restore attempts", ev["restore_attempts"]],
            ["RPO seconds", ev["rpo_rto"].get("rpo_seconds")],
            ["RTO seconds (avg)", ev["rpo_rto"].get("rto_seconds_avg")],
            ["RTO seconds (max)", ev["rpo_rto"].get("rto_seconds_max")],
        ]
        story.append(Paragraph("Evidence Summary", styles["Heading2"]))
        story.append(Table(summary, colWidths=[250, 200],
                           style=TableStyle([("GRID", (0, 0), (-1, -1), 0.25,
                                              colors.grey)])))
        doc.build(story)
        return buf.getvalue(), "application/pdf"


dr_report_generator = DRReportGenerator()
