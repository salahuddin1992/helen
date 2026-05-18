"""
Audit Export Engine.

Async export pipeline that produces signed, verifiable bundles of
audit chain slices in multiple formats:

    jsonl          — raw streaming JSON lines
    jsonl-signed   — JSON lines + sidecar HMAC-SHA-256 of the body
    csv            — CSV with all chain fields
    pdf            — PDF rendering (reportlab if available, else
                     a minimal hand-rolled PDF)
    zip-verifier   — ZIP bundle: jsonl + manifest.json (filters,
                     head seq, head hash, hmac), verifier.py,
                     public-key.pem (placeholder), README.txt

Job state lives in the ``audit_export_jobs`` SQLAlchemy table. Workers
update progress every N rows; the REST endpoint polls status until
``status == "ready"``. The signed-bundle HMAC key is loaded from
settings (``AUDIT_EXPORT_HMAC_KEY``), defaulting to a per-process random
key in development.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import hmac
import io
import json
import os
import secrets
import sqlite3
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.session import async_session_factory
from app.models.audit_export_job import AuditExportJob
from app.services.audit_chain import get_audit_chain

logger = get_logger(__name__)


def _hmac_key() -> bytes:
    """Resolve the HMAC key used to sign exports.

    Prefers ``settings.AUDIT_EXPORT_HMAC_KEY`` (raw or base64 hex);
    otherwise falls back to a per-process random key (development).
    """
    settings = get_settings()
    val = getattr(settings, "AUDIT_EXPORT_HMAC_KEY", None)
    if val:
        if isinstance(val, str):
            return val.encode("utf-8")
        return val
    if not hasattr(_hmac_key, "_dev_key"):
        _hmac_key._dev_key = secrets.token_bytes(32)  # type: ignore[attr-defined]
    return _hmac_key._dev_key  # type: ignore[attr-defined]


def _exports_dir() -> Path:
    settings = get_settings()
    root = Path(getattr(settings, "PROJECT_ROOT", "."))
    p = root / "data" / "audit_exports"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _iter_rows(filters: dict[str, Any]) -> Iterator[sqlite3.Row]:
    """Stream matching rows from the audit chain SQLite DB."""
    chain = get_audit_chain()
    if chain is None:
        return iter(())

    sql = (
        "SELECT seq, timestamp, actor, action, target, payload_json, "
        "payload_hash, prev_hash, chain_hash FROM audit_chain WHERE 1=1"
    )
    params: list[Any] = []
    if filters.get("from_ts") is not None:
        sql += " AND timestamp >= ?"; params.append(filters["from_ts"])
    if filters.get("to_ts") is not None:
        sql += " AND timestamp <= ?"; params.append(filters["to_ts"])
    if filters.get("actor"):
        sql += " AND actor = ?"; params.append(filters["actor"])
    if filters.get("action"):
        sql += " AND action = ?"; params.append(filters["action"])
    if filters.get("resource"):
        sql += " AND target = ?"; params.append(filters["resource"])
    sql += " ORDER BY seq ASC"

    c = sqlite3.connect(
        f"file:{Path(chain.db_path).as_posix()}?mode=ro",
        uri=True, check_same_thread=False,
    )
    c.row_factory = sqlite3.Row
    try:
        yield from c.execute(sql, params)
    finally:
        c.close()


def _count_rows(filters: dict[str, Any]) -> int:
    chain = get_audit_chain()
    if chain is None:
        return 0
    sql = "SELECT COUNT(1) FROM audit_chain WHERE 1=1"
    params: list[Any] = []
    if filters.get("from_ts") is not None:
        sql += " AND timestamp >= ?"; params.append(filters["from_ts"])
    if filters.get("to_ts") is not None:
        sql += " AND timestamp <= ?"; params.append(filters["to_ts"])
    if filters.get("actor"):
        sql += " AND actor = ?"; params.append(filters["actor"])
    if filters.get("action"):
        sql += " AND action = ?"; params.append(filters["action"])
    if filters.get("resource"):
        sql += " AND target = ?"; params.append(filters["resource"])
    c = sqlite3.connect(
        f"file:{Path(chain.db_path).as_posix()}?mode=ro",
        uri=True, check_same_thread=False,
    )
    try:
        row = c.execute(sql, params).fetchone()
        return int(row[0]) if row else 0
    finally:
        c.close()


VERIFIER_SCRIPT = r'''#!/usr/bin/env python3
"""Helen audit-export verifier.

Re-derives each entry's payload_hash and chain_hash from the JSONL
body and compares them to the stored values. If --hmac-key is given,
also verifies the HMAC-SHA-256 over the entire body against the
manifest signature.
"""
from __future__ import annotations
import argparse, hashlib, hmac, json, sys
from pathlib import Path


def hash_payload(p: dict) -> str:
    blob = json.dumps(p, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def link(prev: str, ph: str) -> str:
    return hashlib.sha256((prev + ph).encode("ascii")).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bundle", required=True, type=Path)
    ap.add_argument("--hmac-key", default=None)
    args = ap.parse_args()

    body = (args.bundle / "audit.jsonl").read_bytes()
    manifest = json.loads((args.bundle / "manifest.json").read_text())

    if args.hmac_key:
        sig = hmac.new(args.hmac_key.encode(), body, hashlib.sha256).hexdigest()
        if sig != manifest.get("hmac_sha256"):
            print("HMAC MISMATCH", file=sys.stderr)
            return 2

    last = manifest.get("prev_anchor")
    for i, line in enumerate(body.decode().splitlines(), start=1):
        if not line.strip():
            continue
        rec = json.loads(line)
        ph = hash_payload({
            "ts": rec["timestamp"], "actor": rec["actor"],
            "action": rec["action"], "target": rec.get("target") or rec.get("resource"),
            "payload": rec.get("payload") or {},
        })
        if ph != rec["payload_hash"]:
            print(f"payload_hash mismatch at line {i} (seq={rec['seq']})", file=sys.stderr)
            return 3
        if last and link(last, ph) != rec["chain_hash"]:
            print(f"chain link mismatch at line {i} (seq={rec['seq']})", file=sys.stderr)
            return 4
        last = rec["chain_hash"]
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


class AuditExportEngine:
    """Orchestrator for export jobs. One instance per process."""

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}
        self._cancelled: set[str] = set()

    async def start_export(
        self,
        scope: dict[str, Any],
        format: str,
        filters: dict[str, Any],
        actor_id: str,
    ) -> str:
        """Queue an export. Returns the job_id."""
        if format not in ("jsonl", "jsonl-signed", "csv", "pdf", "zip-verifier"):
            raise ValueError(f"unsupported format: {format}")

        async with async_session_factory() as db:
            row = AuditExportJob(
                actor_id=actor_id,
                scope=dict(scope or {}),
                filters=dict(filters or {}),
                format=format,
                status="queued",
                rows_total=_count_rows(filters or {}),
            )
            db.add(row)
            await db.commit()
            await db.refresh(row)
            job_id = row.id

        loop = asyncio.get_event_loop()
        self._tasks[job_id] = loop.create_task(self._run(job_id))
        logger.info("audit_export_queued",
                    job_id=job_id, format=format, actor=actor_id)
        return job_id

    def cancel(self, job_id: str) -> bool:
        self._cancelled.add(job_id)
        task = self._tasks.get(job_id)
        if task and not task.done():
            task.cancel()
            return True
        return False

    async def get(self, job_id: str) -> Optional[dict[str, Any]]:
        async with async_session_factory() as db:
            res = await db.execute(
                select(AuditExportJob).where(AuditExportJob.id == job_id)
            )
            job = res.scalar_one_or_none()
            return job.to_dict() if job else None

    async def _update(self, db: AsyncSession, job_id: str, **fields: Any) -> None:
        res = await db.execute(
            select(AuditExportJob).where(AuditExportJob.id == job_id)
        )
        job = res.scalar_one_or_none()
        if not job:
            return
        for k, v in fields.items():
            setattr(job, k, v)
        await db.commit()

    async def _run(self, job_id: str) -> None:
        try:
            async with async_session_factory() as db:
                res = await db.execute(
                    select(AuditExportJob).where(AuditExportJob.id == job_id)
                )
                job = res.scalar_one_or_none()
                if not job:
                    return
                fmt = job.format
                filters = dict(job.filters or {})
                await self._update(db, job_id,
                                   status="running",
                                   started_at=datetime.now(timezone.utc))

            # Run the heavy work in a worker thread so we don't block the loop
            file_path, sha256, hmac_sig, rows = await asyncio.to_thread(
                self._produce, job_id, fmt, filters,
            )

            async with async_session_factory() as db:
                await self._update(
                    db, job_id,
                    status="ready",
                    progress=100,
                    rows_processed=rows,
                    file_path=str(file_path),
                    file_size=file_path.stat().st_size if file_path.exists() else 0,
                    sha256=sha256,
                    hmac_signature=hmac_sig,
                    completed_at=datetime.now(timezone.utc),
                )
            logger.info("audit_export_ready", job_id=job_id, rows=rows)
        except asyncio.CancelledError:
            async with async_session_factory() as db:
                await self._update(db, job_id, status="cancelled")
            raise
        except Exception as exc:
            logger.exception("audit_export_failed", job_id=job_id)
            async with async_session_factory() as db:
                await self._update(
                    db, job_id, status="failed", error_message=str(exc),
                )
        finally:
            self._tasks.pop(job_id, None)
            self._cancelled.discard(job_id)

    # ── producers ────────────────────────────────────────────────────

    def _produce(
        self, job_id: str, fmt: str, filters: dict[str, Any],
    ) -> tuple[Path, str, Optional[str], int]:
        if fmt == "jsonl":
            return self._produce_jsonl(job_id, filters, signed=False)
        if fmt == "jsonl-signed":
            return self._produce_jsonl(job_id, filters, signed=True)
        if fmt == "csv":
            return self._produce_csv(job_id, filters)
        if fmt == "pdf":
            return self._produce_pdf(job_id, filters)
        if fmt == "zip-verifier":
            return self._produce_zip_verifier(job_id, filters)
        raise ValueError(fmt)

    def _produce_jsonl(
        self, job_id: str, filters: dict[str, Any], *, signed: bool,
    ) -> tuple[Path, str, Optional[str], int]:
        out = _exports_dir() / f"{job_id}.jsonl"
        sha = hashlib.sha256()
        mac = hmac.new(_hmac_key(), digestmod=hashlib.sha256) if signed else None
        rows = 0
        with out.open("wb") as f:
            for r in _iter_rows(filters):
                rec = {
                    "seq": r["seq"], "timestamp": r["timestamp"],
                    "actor": r["actor"], "action": r["action"],
                    "resource": r["target"],
                    "payload": json.loads(r["payload_json"] or "{}"),
                    "payload_hash": r["payload_hash"],
                    "prev_hash": r["prev_hash"],
                    "chain_hash": r["chain_hash"],
                }
                line = (json.dumps(rec, ensure_ascii=False) + "\n").encode()
                f.write(line)
                sha.update(line)
                if mac is not None:
                    mac.update(line)
                rows += 1
                if rows % 5000 == 0:
                    self._tick(job_id, rows)
        sig = mac.hexdigest() if mac else None
        return out, sha.hexdigest(), sig, rows

    def _produce_csv(
        self, job_id: str, filters: dict[str, Any],
    ) -> tuple[Path, str, Optional[str], int]:
        out = _exports_dir() / f"{job_id}.csv"
        sha = hashlib.sha256()
        rows = 0
        with out.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["seq", "timestamp", "actor", "action", "resource",
                        "payload", "payload_hash", "prev_hash", "chain_hash"])
            buf = io.StringIO()
            for r in _iter_rows(filters):
                buf.seek(0); buf.truncate()
                wb = csv.writer(buf)
                wb.writerow([
                    r["seq"], r["timestamp"], r["actor"], r["action"],
                    r["target"] or "", r["payload_json"],
                    r["payload_hash"], r["prev_hash"], r["chain_hash"],
                ])
                line = buf.getvalue()
                f.write(line)
                sha.update(line.encode())
                rows += 1
                if rows % 5000 == 0:
                    self._tick(job_id, rows)
        return out, sha.hexdigest(), None, rows

    def _produce_pdf(
        self, job_id: str, filters: dict[str, Any],
    ) -> tuple[Path, str, Optional[str], int]:
        rows_list = list(_iter_rows(filters))
        out = _exports_dir() / f"{job_id}.pdf"
        try:
            from reportlab.lib.pagesizes import A4  # type: ignore
            from reportlab.pdfgen import canvas as _canvas  # type: ignore
            c = _canvas.Canvas(str(out), pagesize=A4)
            w, h = A4
            y = h - 40
            c.setFont("Helvetica-Bold", 12)
            c.drawString(40, y, "Helen — Audit Chain Export")
            y -= 20
            c.setFont("Helvetica", 8)
            for r in rows_list:
                if y < 60:
                    c.showPage(); y = h - 40; c.setFont("Helvetica", 8)
                ts = datetime.fromtimestamp(r["timestamp"], tz=timezone.utc).isoformat()
                line = (f"#{r['seq']:>6}  {ts}  actor={(r['actor'] or '')[:14]}  "
                        f"action={(r['action'] or '')[:28]}  "
                        f"target={(r['target'] or '')[:28]}")
                c.drawString(40, y, line)
                y -= 11
            c.save()
        except ImportError:
            # Fallback: minimal PDF
            self._write_plain_pdf(out, rows_list)
        data = out.read_bytes()
        return out, hashlib.sha256(data).hexdigest(), None, len(rows_list)

    def _write_plain_pdf(self, out: Path, rows: list[sqlite3.Row]) -> None:
        lines = ["Helen — Audit Chain Export", ""]
        for r in rows[:120]:
            ts = datetime.fromtimestamp(r["timestamp"], tz=timezone.utc).isoformat()
            lines.append(
                f"#{r['seq']} {ts} actor={r['actor']} action={r['action']} "
                f"target={r['target'] or '-'}"
            )

        def esc(s: str) -> str:
            return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

        content_lines = ["BT", "/F1 9 Tf", "40 800 Td", "14 TL"]
        for ln in lines:
            content_lines.append(f"({esc(ln)}) Tj T*")
        content_lines.append("ET")
        stream = "\n".join(content_lines).encode("latin-1", errors="replace")

        objects: list[bytes] = []
        objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
        objects.append(b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
        objects.append(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 842] "
                       b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>")
        objects.append(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n"
                       + stream + b"\nendstream")
        objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>")

        buf = io.BytesIO()
        buf.write(b"%PDF-1.4\n")
        offsets: list[int] = []
        for i, obj in enumerate(objects, start=1):
            offsets.append(buf.tell())
            buf.write(f"{i} 0 obj\n".encode())
            buf.write(obj)
            buf.write(b"\nendobj\n")
        xref_pos = buf.tell()
        buf.write(f"xref\n0 {len(objects)+1}\n".encode())
        buf.write(b"0000000000 65535 f \n")
        for off in offsets:
            buf.write(f"{off:010d} 00000 n \n".encode())
        buf.write(b"trailer\n")
        buf.write(f"<< /Size {len(objects)+1} /Root 1 0 R >>\n".encode())
        buf.write(b"startxref\n")
        buf.write(f"{xref_pos}\n".encode())
        buf.write(b"%%EOF")
        out.write_bytes(buf.getvalue())

    def _produce_zip_verifier(
        self, job_id: str, filters: dict[str, Any],
    ) -> tuple[Path, str, Optional[str], int]:
        # Build the JSONL body in memory (operators typically scope to
        # specific cases; if the chain is huge they'd pick jsonl-signed).
        body = io.BytesIO()
        mac = hmac.new(_hmac_key(), digestmod=hashlib.sha256)
        prev_anchor: Optional[str] = None
        rows = 0
        for r in _iter_rows(filters):
            if prev_anchor is None:
                prev_anchor = r["prev_hash"]
            rec = {
                "seq": r["seq"], "timestamp": r["timestamp"],
                "actor": r["actor"], "action": r["action"],
                "resource": r["target"],
                "payload": json.loads(r["payload_json"] or "{}"),
                "payload_hash": r["payload_hash"],
                "prev_hash": r["prev_hash"],
                "chain_hash": r["chain_hash"],
            }
            line = (json.dumps(rec, ensure_ascii=False) + "\n").encode()
            body.write(line); mac.update(line)
            rows += 1
            if rows % 5000 == 0:
                self._tick(job_id, rows)

        sig = mac.hexdigest()
        manifest = {
            "job_id": job_id,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "filters": filters,
            "rows": rows,
            "hmac_sha256": sig,
            "prev_anchor": prev_anchor,
            "format_version": 1,
        }

        out = _exports_dir() / f"{job_id}.zip"
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("audit.jsonl", body.getvalue())
            z.writestr("manifest.json", json.dumps(manifest, indent=2))
            z.writestr("verifier.py", VERIFIER_SCRIPT)
            z.writestr("README.txt",
                       "Helen audit export bundle.\n"
                       "Run: python verifier.py --bundle .\n"
                       "Optional: --hmac-key <hex/raw>\n")
            z.writestr("public-key.pem", "-----BEGIN PLACEHOLDER-----\n")
        data = out.read_bytes()
        return out, hashlib.sha256(data).hexdigest(), sig, rows

    def _tick(self, job_id: str, rows: int) -> None:
        """Synchronous progress update from worker thread — fire-and-forget
        coroutine scheduled on the main loop."""
        try:
            loop = asyncio.get_event_loop_policy().get_event_loop()
            if loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    self._tick_async(job_id, rows), loop,
                )
        except Exception:
            pass

    async def _tick_async(self, job_id: str, rows: int) -> None:
        async with async_session_factory() as db:
            res = await db.execute(
                select(AuditExportJob).where(AuditExportJob.id == job_id)
            )
            job = res.scalar_one_or_none()
            if not job:
                return
            job.rows_processed = rows
            if job.rows_total > 0:
                job.progress = min(99, int(rows * 100 / max(1, job.rows_total)))
            await db.commit()


_engine: Optional[AuditExportEngine] = None


def get_export_engine() -> AuditExportEngine:
    global _engine
    if _engine is None:
        _engine = AuditExportEngine()
    return _engine


__all__ = ["AuditExportEngine", "get_export_engine"]
