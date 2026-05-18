"""
Admin — Audit chain viewer (Phase 2 / Module H).

Endpoints
---------
GET /api/admin/audit/events    — paginated, filtered query (cursor-based)
GET /api/admin/audit/verify    — walk the hash chain, return first break
GET /api/admin/audit/export    — streaming export (jsonl / csv / pdf)
GET /api/admin/audit/stats     — counts by action, actor, day

Backed by ``app.services.audit_chain.AuditChain``. If the chain singleton
isn't configured yet we try to locate the SQLite file at the conventional
``<data>/audit_chain.db`` path and open it read-only.
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Iterable, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.security_utils import require_role
from app.services.audit_chain import get_audit_chain

logger = get_logger(__name__)

router = APIRouter(prefix="/admin/audit", tags=["admin-phase2"])


# ── Helpers ──────────────────────────────────────────────

def _audit_db_path() -> Path:
    """Locate the audit chain SQLite. Try the runtime singleton first,
    fall back to conventional locations."""
    chain = get_audit_chain()
    if chain is not None and getattr(chain, "db_path", None):
        return Path(chain.db_path)
    settings = get_settings()
    root = Path(settings.PROJECT_ROOT)
    candidates = [
        root / "data" / "audit_chain.db",
        root / "audit_chain.db",
        Path("./data/audit_chain.db"),
    ]
    for p in candidates:
        if p.exists():
            return p
    raise HTTPException(
        status_code=503,
        detail="audit_chain database not initialised on this server",
    )


def _ro_connect(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.as_posix()}?mode=ro"
    c = sqlite3.connect(uri, uri=True, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    try:
        d["payload"] = json.loads(d.pop("payload_json", "{}") or "{}")
    except Exception:
        d["payload"] = {"_raw": d.pop("payload_json", "")}
    return d


# ── Models ───────────────────────────────────────────────

class AuditEventOut(BaseModel):
    seq: int
    timestamp: float
    actor: str
    action: str
    target: Optional[str]
    payload: dict[str, Any]
    payload_hash: str
    prev_hash: str
    chain_hash: str


# ── Query ────────────────────────────────────────────────

@router.get("/events")
async def list_events(
    user_id: str = Depends(require_role("admin")),
    cursor: Optional[int] = Query(None, description="seq to paginate before"),
    limit: int = Query(100, ge=1, le=1000),
    actor: Optional[str] = None,
    action: Optional[str] = None,
    target: Optional[str] = None,
    since: Optional[float] = None,
    until: Optional[float] = None,
    q: Optional[str] = Query(None, description="substring search on payload_json"),
):
    path = _audit_db_path()
    sql = (
        "SELECT seq, timestamp, actor, action, target, "
        "payload_json, payload_hash, prev_hash, chain_hash "
        "FROM audit_chain WHERE 1=1"
    )
    params: list[Any] = []
    if cursor is not None:
        sql += " AND seq < ?"
        params.append(cursor)
    if actor:
        sql += " AND actor = ?"
        params.append(actor)
    if action:
        sql += " AND action = ?"
        params.append(action)
    if target:
        sql += " AND target = ?"
        params.append(target)
    if since is not None:
        sql += " AND timestamp >= ?"
        params.append(since)
    if until is not None:
        sql += " AND timestamp <= ?"
        params.append(until)
    if q:
        sql += " AND payload_json LIKE ?"
        params.append(f"%{q}%")
    sql += " ORDER BY seq DESC LIMIT ?"
    params.append(limit)

    with _ro_connect(path) as c:
        rows = c.execute(sql, params).fetchall()

    events = [_row_to_dict(r) for r in rows]
    next_cursor = events[-1]["seq"] if events and len(events) == limit else None
    return {
        "events": events,
        "count": len(events),
        "next_cursor": next_cursor,
    }


@router.get("/verify")
async def verify_chain(
    user_id: str = Depends(require_role("admin")),
    from_seq: Optional[int] = Query(None, alias="from_id"),
    to_seq: Optional[int] = Query(None, alias="to_id"),
):
    """Walk the hash chain and report integrity status. If ``from_id`` /
    ``to_id`` are unset we use the runtime ``AuditChain.verify()`` which
    walks the whole table. Otherwise we re-implement the walk inline so
    we can scope it to a slice."""
    chain = get_audit_chain()
    if chain is not None and from_seq is None and to_seq is None:
        ok, broken_at, msg = chain.verify()
        return {"ok": ok, "broken_at": broken_at, "message": msg}

    # Bounded re-verify
    path = _audit_db_path()
    import hashlib
    GENESIS = hashlib.sha256(b"GENESIS-helen-audit-v1").hexdigest()

    def _hash_payload(p: dict) -> str:
        blob = json.dumps(p, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()

    def _link(prev: str, ph: str) -> str:
        return hashlib.sha256((prev + ph).encode("ascii")).hexdigest()

    where = "WHERE 1=1"
    params: list[Any] = []
    if from_seq is not None:
        where += " AND seq >= ?"; params.append(from_seq)
    if to_seq is not None:
        where += " AND seq <= ?"; params.append(to_seq)

    expected_prev = GENESIS
    last_seq = (from_seq - 1) if from_seq else 0

    with _ro_connect(path) as c:
        # If the slice doesn't start at 1, seed expected_prev with the
        # preceding row's chain_hash.
        if from_seq and from_seq > 1:
            prev_row = c.execute(
                "SELECT chain_hash FROM audit_chain WHERE seq = ?",
                (from_seq - 1,),
            ).fetchone()
            if prev_row is None:
                return {"ok": False, "broken_at": from_seq,
                        "message": f"no row before seq={from_seq}"}
            expected_prev = prev_row["chain_hash"]

        rows = c.execute(
            "SELECT seq, timestamp, actor, action, target, payload_json, "
            "payload_hash, prev_hash, chain_hash "
            f"FROM audit_chain {where} ORDER BY seq ASC", params
        )
        for row in rows:
            seq = row["seq"]
            if seq != last_seq + 1:
                return {"ok": False, "broken_at": seq,
                        "message": f"sequence gap at seq={seq}"}
            last_seq = seq
            if row["prev_hash"] != expected_prev:
                return {"ok": False, "broken_at": seq,
                        "message": f"prev_hash mismatch at seq={seq}"}
            computed_ph = _hash_payload({
                "ts": row["timestamp"], "actor": row["actor"],
                "action": row["action"], "target": row["target"],
                "payload": json.loads(row["payload_json"]),
            })
            if computed_ph != row["payload_hash"]:
                return {"ok": False, "broken_at": seq,
                        "message": f"payload_hash mismatch at seq={seq}"}
            if _link(expected_prev, row["payload_hash"]) != row["chain_hash"]:
                return {"ok": False, "broken_at": seq,
                        "message": f"chain_hash mismatch at seq={seq}"}
            expected_prev = row["chain_hash"]

    return {"ok": True, "broken_at": None,
            "message": f"chain_intact (last_seq={last_seq})"}


@router.get("/stats")
async def stats(
    user_id: str = Depends(require_role("admin")),
    since: Optional[float] = None,
    days: int = Query(14, ge=1, le=365),
):
    """Counts by action and actor; daily histogram."""
    path = _audit_db_path()
    cutoff = since if since is not None else (time.time() - days * 86400)

    by_action: Counter[str] = Counter()
    by_actor: Counter[str] = Counter()
    by_day: defaultdict[str, int] = defaultdict(int)
    total = 0

    with _ro_connect(path) as c:
        for row in c.execute(
            "SELECT timestamp, actor, action FROM audit_chain WHERE timestamp >= ?",
            (cutoff,),
        ):
            total += 1
            by_action[row["action"]] += 1
            by_actor[row["actor"]] += 1
            day = datetime.fromtimestamp(row["timestamp"],
                                         tz=timezone.utc).strftime("%Y-%m-%d")
            by_day[day] += 1

        head = c.execute(
            "SELECT seq, timestamp, chain_hash FROM audit_chain "
            "ORDER BY seq DESC LIMIT 1"
        ).fetchone()

    return {
        "since": cutoff,
        "total": total,
        "by_action": by_action.most_common(50),
        "by_actor": by_actor.most_common(50),
        "by_day": sorted(by_day.items()),
        "head": dict(head) if head else None,
    }


# ── Export ───────────────────────────────────────────────

@router.get("/export")
async def export_chain(
    user_id: str = Depends(require_role("admin")),
    format: str = Query("jsonl", pattern="^(jsonl|csv|pdf)$"),
    since: Optional[float] = None,
    until: Optional[float] = None,
):
    path = _audit_db_path()
    fname = f"helen-audit-{int(time.time())}.{format}"

    sql = ("SELECT seq, timestamp, actor, action, target, "
           "payload_json, payload_hash, prev_hash, chain_hash "
           "FROM audit_chain WHERE 1=1")
    params: list[Any] = []
    if since is not None:
        sql += " AND timestamp >= ?"; params.append(since)
    if until is not None:
        sql += " AND timestamp <= ?"; params.append(until)
    sql += " ORDER BY seq ASC"

    def _iter_rows() -> Iterable[sqlite3.Row]:
        # Hold the connection open for the duration of the stream.
        c = _ro_connect(path)
        try:
            yield from c.execute(sql, params)
        finally:
            c.close()

    if format == "jsonl":
        async def gen() -> AsyncIterator[bytes]:
            for r in _iter_rows():
                d = _row_to_dict(r)
                yield (json.dumps(d, ensure_ascii=False, default=str) + "\n").encode()
        return StreamingResponse(
            gen(), media_type="application/x-jsonlines",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )

    if format == "csv":
        async def gen_csv() -> AsyncIterator[bytes]:
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["seq", "timestamp", "actor", "action", "target",
                        "payload", "payload_hash", "prev_hash", "chain_hash"])
            yield buf.getvalue().encode()
            for r in _iter_rows():
                buf.seek(0); buf.truncate()
                w.writerow([
                    r["seq"], r["timestamp"], r["actor"], r["action"],
                    r["target"] or "", r["payload_json"],
                    r["payload_hash"], r["prev_hash"], r["chain_hash"],
                ])
                yield buf.getvalue().encode()
        return StreamingResponse(
            gen_csv(), media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )

    # PDF — dependency-free minimal PDF (single text page per chunk).
    # We deliberately avoid pulling reportlab; this produces a readable
    # but very plain document. If reportlab is installed, we prefer it.
    rows = list(_iter_rows())
    try:
        return _pdf_with_reportlab(rows, fname)
    except ImportError:
        return _pdf_plain(rows, fname)


def _pdf_with_reportlab(rows: list[sqlite3.Row], fname: str) -> Response:
    from reportlab.lib.pagesizes import A4                                # type: ignore
    from reportlab.pdfgen import canvas as _canvas                        # type: ignore
    buf = io.BytesIO()
    c = _canvas.Canvas(buf, pagesize=A4)
    w, h = A4
    y = h - 40
    c.setFont("Helvetica-Bold", 12)
    c.drawString(40, y, "Helen — Audit Chain Export")
    y -= 24
    c.setFont("Helvetica", 8)
    for r in rows:
        if y < 60:
            c.showPage(); y = h - 40; c.setFont("Helvetica", 8)
        ts = datetime.fromtimestamp(r["timestamp"], tz=timezone.utc).isoformat()
        line = (f"#{r['seq']:>6}  {ts}  actor={r['actor'][:14]}  "
                f"action={r['action'][:30]}  target={(r['target'] or '')[:30]}")
        c.drawString(40, y, line)
        y -= 11
    c.save()
    return Response(
        content=buf.getvalue(),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


def _pdf_plain(rows: list[sqlite3.Row], fname: str) -> Response:
    """Hand-rolled minimal PDF when reportlab isn't installed."""
    lines: list[str] = ["Helen — Audit Chain Export", ""]
    for r in rows:
        ts = datetime.fromtimestamp(r["timestamp"], tz=timezone.utc).isoformat()
        lines.append(
            f"#{r['seq']}  {ts}  actor={r['actor']}  action={r['action']}  "
            f"target={r['target'] or '-'}"
        )

    # Build a single-page PDF with one Tj per line. Good enough as a
    # graceful-degradation; users get a real PDF without extra deps.
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    content_lines = ["BT", "/F1 9 Tf", "40 800 Td", "14 TL"]
    for ln in lines[:120]:        # one page only — point users to JSONL for huge sets
        content_lines.append(f"({esc(ln)}) Tj T*")
    content_lines.append("ET")
    stream = "\n".join(content_lines).encode("latin-1", errors="replace")

    objects: list[bytes] = []
    def add(obj: bytes) -> int:
        objects.append(obj); return len(objects)

    add(b"<< /Type /Catalog /Pages 2 0 R >>")
    add(b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    add(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 842] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>")
    add(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n"
        + stream + b"\nendstream")
    add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>")

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

    return Response(
        content=buf.getvalue(),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )
