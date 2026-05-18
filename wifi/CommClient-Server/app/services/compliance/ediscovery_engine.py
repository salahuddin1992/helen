"""
EDiscoveryEngine — full-text search + case management.

Capabilities:
* Lucene-style query parser: AND/OR/NOT, "quoted phrases", wildcard*,
  fuzzy~ markers (best-effort), field:value.
* Cross-resource search: messages, files, calls, audit, presence.
* Scored results with snippet generation.
* Faceted aggregation (by resource_type, by channel, by date_bucket).
* Case CRUD + evidence pool with tags.

Backend strategy
----------------
We deliberately avoid pulling in a heavy search engine (no Elastic, no
Lucene). Instead we issue parallel SQL queries against each source
table, score by token frequency and recency, and aggregate. For the
volumes typical of CommClient (millions of rows, tens of thousands of
matches per query) this is fast enough; for larger deployments the
interface is stable so an Elastic backend can be dropped in.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy import and_, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_log
from app.core.logging import get_logger
from app.db.session import async_session_factory
from app.models.compliance_case import (
    VALID_CASE_STATUSES,
    VALID_EVIDENCE_TAGS,
    ComplianceCase,
    ComplianceCaseEvidence,
)

logger = get_logger(__name__)


# ── parser ──────────────────────────────────────────────────────


_TOKEN_RE = re.compile(
    r'''(?P<not>NOT\s+)?
        (?:"(?P<phrase>[^"]+)"
        |(?P<field>\w+):(?P<value>[^\s)]+)
        |(?P<word>[\w\*\?\-\.]+))''',
    re.VERBOSE,
)


@dataclass
class QueryClause:
    op: str = "AND"        # connector to previous clause: AND/OR
    negate: bool = False
    phrase: Optional[str] = None
    word: Optional[str] = None
    field: Optional[str] = None


@dataclass
class ParsedQuery:
    clauses: List[QueryClause] = field(default_factory=list)
    raw: str = ""


def parse_query(q: str) -> ParsedQuery:
    """Parse a Lucene-lite query string into clauses."""
    if not q:
        return ParsedQuery(raw="")
    parsed = ParsedQuery(raw=q)
    parts = re.split(r"\s+(AND|OR)\s+", q)
    # parts alternates: clause, OP, clause, OP, ...
    pending_op = "AND"
    for idx, part in enumerate(parts):
        if part in ("AND", "OR"):
            pending_op = part
            continue
        token = part.strip()
        if not token:
            continue
        negate = False
        if token.startswith("NOT "):
            negate = True
            token = token[4:].strip()
        elif token.startswith("-") and len(token) > 1:
            negate = True
            token = token[1:]
        m = _TOKEN_RE.search(token)
        if not m:
            parsed.clauses.append(QueryClause(
                op=pending_op, negate=negate, word=token,
            ))
            continue
        if m.group("phrase"):
            parsed.clauses.append(QueryClause(
                op=pending_op, negate=negate, phrase=m.group("phrase"),
            ))
        elif m.group("field"):
            parsed.clauses.append(QueryClause(
                op=pending_op, negate=negate,
                field=m.group("field"), word=m.group("value"),
            ))
        else:
            parsed.clauses.append(QueryClause(
                op=pending_op, negate=negate, word=m.group("word"),
            ))
        pending_op = "AND"  # reset until we see OP again
    return parsed


def _like_pattern(needle: str) -> str:
    """Convert wildcard syntax * / ? into SQL LIKE patterns."""
    if not needle:
        return "%"
    n = needle.replace("%", r"\%").replace("_", r"\_")
    n = n.replace("*", "%").replace("?", "_")
    if "%" not in n and "_" not in n:
        n = f"%{n}%"
    return n


def _snippet(text: str, terms: Iterable[str], *, width: int = 80) -> str:
    if not text:
        return ""
    t = text
    lower = t.lower()
    for term in terms:
        if not term:
            continue
        idx = lower.find(term.lower().strip("*?"))
        if idx >= 0:
            start = max(0, idx - width // 2)
            end = min(len(t), idx + width)
            seg = t[start:end].replace("\n", " ")
            if start > 0:
                seg = "…" + seg
            if end < len(t):
                seg = seg + "…"
            return seg
    return (t[:160] + ("…" if len(t) > 160 else "")).replace("\n", " ")


def _score(text: str, terms: Iterable[str], *, ts: Optional[datetime] = None) -> float:
    if not text:
        return 0.0
    lower = text.lower()
    s = 0.0
    for term in terms:
        if not term:
            continue
        s += lower.count(term.lower().strip("*?")) * 1.5
    if ts:
        age_days = (datetime.now(timezone.utc) - ts).days if ts else 365
        s += max(0.0, 1.0 - (age_days / 3650.0))
    return s


# ── engine ──────────────────────────────────────────────────────


class EDiscoveryEngine:
    """Full-text search + case management."""

    SUPPORTED_SOURCES = (
        "messages", "files", "calls", "audit", "presence",
    )

    # ── search ─────────────────────────────────────────────

    async def search(
        self,
        db: AsyncSession,
        *,
        q: str,
        filters: Optional[Dict[str, Any]] = None,
        sort: str = "relevance",
        limit: int = 50,
        offset: int = 0,
    ) -> Dict[str, Any]:
        parsed = parse_query(q or "")
        filters = filters or {}
        sources = filters.get("sources") or list(self.SUPPORTED_SOURCES)
        results: List[Dict[str, Any]] = []
        facets: Dict[str, Dict[str, int]] = {
            "resource_type": {}, "channel": {}, "date_bucket": {},
        }

        terms = [c.phrase or c.word for c in parsed.clauses if not c.negate]
        terms = [t for t in terms if t]

        for src in sources:
            try:
                rows = await self._search_source(
                    db, src, parsed, filters, limit_per_source=limit * 2,
                )
            except Exception as e:
                logger.warning("ediscovery_source_failed", src=src, error=str(e))
                continue
            for r in rows:
                results.append(r)
                facets["resource_type"][r["resource_type"]] = (
                    facets["resource_type"].get(r["resource_type"], 0) + 1
                )
                ch = r.get("channel_id") or "—"
                facets["channel"][ch] = facets["channel"].get(ch, 0) + 1
                ts = r.get("timestamp")
                bucket = (ts or "")[:7] if isinstance(ts, str) else "—"
                facets["date_bucket"][bucket] = facets["date_bucket"].get(bucket, 0) + 1

        if sort == "recent":
            results.sort(key=lambda x: x.get("timestamp") or "", reverse=True)
        elif sort == "oldest":
            results.sort(key=lambda x: x.get("timestamp") or "")
        else:
            results.sort(key=lambda x: x.get("score", 0.0), reverse=True)

        total = len(results)
        page = results[offset:offset + limit]
        return {
            "total": total, "limit": limit, "offset": offset,
            "items": page, "facets": facets, "parsed_clauses": [
                {"op": c.op, "negate": c.negate, "phrase": c.phrase,
                 "word": c.word, "field": c.field}
                for c in parsed.clauses
            ],
        }

    async def _search_source(
        self,
        db: AsyncSession,
        source: str,
        parsed: ParsedQuery,
        filters: Dict[str, Any],
        *,
        limit_per_source: int,
    ) -> List[Dict[str, Any]]:
        if source == "messages":
            return await self._search_messages(db, parsed, filters, limit_per_source)
        if source == "files":
            return await self._search_files(db, parsed, filters, limit_per_source)
        if source == "calls":
            return await self._search_calls(db, parsed, filters, limit_per_source)
        if source == "audit":
            return await self._search_audit(db, parsed, filters, limit_per_source)
        return []

    async def _search_messages(
        self, db, parsed, filters, limit,
    ) -> List[Dict[str, Any]]:
        try:
            from app.models.message import Message
        except Exception:
            return []
        q = select(Message)
        for c in parsed.clauses:
            needle = c.phrase or c.word
            if not needle:
                continue
            col = Message.content if hasattr(Message, "content") else None
            if c.field and hasattr(Message, c.field):
                col = getattr(Message, c.field)
            if col is None:
                continue
            cond = col.ilike(_like_pattern(needle))
            if c.negate:
                q = q.where(~cond)
            else:
                q = q.where(cond)
        if filters.get("channel_ids"):
            q = q.where(Message.channel_id.in_(filters["channel_ids"]))
        if filters.get("sender_ids"):
            q = q.where(Message.sender_id.in_(filters["sender_ids"]))
        if filters.get("date_from"):
            q = q.where(Message.created_at >= filters["date_from"])
        if filters.get("date_to"):
            q = q.where(Message.created_at <= filters["date_to"])
        q = q.order_by(desc(Message.created_at)).limit(limit)
        rows = (await db.execute(q)).scalars().all()
        terms = [c.phrase or c.word for c in parsed.clauses if not c.negate and (c.phrase or c.word)]
        out: List[Dict[str, Any]] = []
        for r in rows:
            content = getattr(r, "content", "") or ""
            ts = getattr(r, "created_at", None)
            out.append({
                "resource_type": "messages",
                "resource_id": str(r.id),
                "score": _score(content, terms, ts=ts),
                "snippet": _snippet(content, terms),
                "channel_id": getattr(r, "channel_id", None),
                "sender_id": getattr(r, "sender_id", None),
                "timestamp": ts.isoformat() if ts else None,
            })
        return out

    async def _search_files(
        self, db, parsed, filters, limit,
    ) -> List[Dict[str, Any]]:
        try:
            from app.models.file import FileRecord
        except Exception:
            return []
        q = select(FileRecord)
        for c in parsed.clauses:
            needle = c.phrase or c.word
            if not needle:
                continue
            col = None
            if c.field and hasattr(FileRecord, c.field):
                col = getattr(FileRecord, c.field)
            elif hasattr(FileRecord, "filename"):
                col = FileRecord.filename
            if col is None:
                continue
            cond = col.ilike(_like_pattern(needle))
            if c.negate:
                q = q.where(~cond)
            else:
                q = q.where(cond)
        q = q.limit(limit)
        rows = (await db.execute(q)).scalars().all()
        terms = [c.phrase or c.word for c in parsed.clauses if not c.negate and (c.phrase or c.word)]
        out: List[Dict[str, Any]] = []
        for r in rows:
            fn = getattr(r, "filename", "") or ""
            ts = getattr(r, "created_at", None) or getattr(r, "uploaded_at", None)
            out.append({
                "resource_type": "files",
                "resource_id": str(r.id),
                "score": _score(fn, terms, ts=ts),
                "snippet": _snippet(fn, terms),
                "uploader_id": getattr(r, "uploader_id", None),
                "timestamp": ts.isoformat() if ts else None,
            })
        return out

    async def _search_calls(
        self, db, parsed, filters, limit,
    ) -> List[Dict[str, Any]]:
        try:
            from app.models.call_log import CallLog
        except Exception:
            return []
        q = select(CallLog).limit(limit)
        rows = (await db.execute(q)).scalars().all()
        terms = [c.phrase or c.word for c in parsed.clauses if not c.negate and (c.phrase or c.word)]
        out: List[Dict[str, Any]] = []
        for r in rows:
            blob = " ".join(str(getattr(r, attr, "") or "")
                            for attr in ("caller_id", "callee_id", "channel_id", "status"))
            if terms and not any(t.lower() in blob.lower() for t in terms):
                continue
            ts = getattr(r, "created_at", None)
            out.append({
                "resource_type": "calls",
                "resource_id": str(r.id),
                "score": _score(blob, terms, ts=ts),
                "snippet": _snippet(blob, terms),
                "channel_id": getattr(r, "channel_id", None),
                "timestamp": ts.isoformat() if ts else None,
            })
        return out

    async def _search_audit(
        self, db, parsed, filters, limit,
    ) -> List[Dict[str, Any]]:
        try:
            from app.models.audit_log import AuditLog
        except Exception:
            return []
        q = select(AuditLog)
        for c in parsed.clauses:
            needle = c.phrase or c.word
            if not needle:
                continue
            col = None
            if c.field and hasattr(AuditLog, c.field):
                col = getattr(AuditLog, c.field)
            else:
                col = AuditLog.event
            cond = col.ilike(_like_pattern(needle))
            if c.negate:
                q = q.where(~cond)
            else:
                q = q.where(cond)
        q = q.order_by(desc(getattr(AuditLog, "occurred_at", AuditLog.id))).limit(limit)
        rows = (await db.execute(q)).scalars().all()
        terms = [c.phrase or c.word for c in parsed.clauses if not c.negate and (c.phrase or c.word)]
        out: List[Dict[str, Any]] = []
        for r in rows:
            blob = f"{getattr(r, 'event', '')} {getattr(r, 'user_id', '')} {getattr(r, 'details_json', '') or ''}"
            ts = getattr(r, "occurred_at", None) or getattr(r, "created_at", None)
            out.append({
                "resource_type": "audit",
                "resource_id": str(r.id),
                "score": _score(blob, terms, ts=ts),
                "snippet": _snippet(blob, terms),
                "timestamp": ts.isoformat() if ts else None,
                "actor_id": getattr(r, "user_id", None),
            })
        return out

    # ── cases ──────────────────────────────────────────────

    async def list_cases(
        self, db: AsyncSession, *,
        status: Optional[str] = None,
        search: Optional[str] = None,
        limit: int = 100, offset: int = 0,
    ) -> List[ComplianceCase]:
        q = select(ComplianceCase)
        if status:
            q = q.where(ComplianceCase.status == status)
        if search:
            s = f"%{search}%"
            q = q.where(or_(
                ComplianceCase.name.ilike(s),
                ComplianceCase.matter_number.ilike(s),
                ComplianceCase.description.ilike(s),
            ))
        q = q.order_by(desc(ComplianceCase.created_at)).offset(offset).limit(limit)
        return list((await db.execute(q)).scalars().all())

    async def get_case(self, db: AsyncSession, case_id: str) -> Optional[ComplianceCase]:
        return (await db.execute(
            select(ComplianceCase).where(ComplianceCase.id == case_id)
        )).scalar_one_or_none()

    async def create_case(
        self, db: AsyncSession, *,
        name: str,
        matter_number: Optional[str],
        description: Optional[str],
        custodians: List[str],
        hold_id: Optional[str],
        actor_id: str,
    ) -> ComplianceCase:
        c = ComplianceCase(
            id=uuid.uuid4().hex,
            name=name, matter_number=matter_number,
            description=description, custodians=custodians or [],
            hold_id=hold_id, owner_id=actor_id,
        )
        db.add(c)
        await db.commit()
        audit_log("compliance.case_created", user_id=actor_id, success=True,
                  details={"case_id": c.id, "name": name})
        return c

    async def update_case(
        self, db: AsyncSession, case_id: str, *,
        patch: Dict[str, Any], actor_id: str,
    ) -> ComplianceCase:
        c = await self.get_case(db, case_id)
        if c is None:
            raise LookupError(case_id)
        for k, v in patch.items():
            if hasattr(c, k) and v is not None:
                setattr(c, k, v)
        await db.commit()
        audit_log("compliance.case_updated", user_id=actor_id, success=True,
                  details={"case_id": c.id})
        return c

    async def delete_case(
        self, db: AsyncSession, case_id: str, *, actor_id: str,
    ) -> None:
        c = await self.get_case(db, case_id)
        if c is None:
            raise LookupError(case_id)
        await db.delete(c)
        await db.commit()
        audit_log("compliance.case_deleted", user_id=actor_id, success=True,
                  details={"case_id": case_id})

    async def add_evidence(
        self, db: AsyncSession, case_id: str, *,
        items: List[Dict[str, Any]], actor_id: str,
    ) -> Dict[str, Any]:
        c = await self.get_case(db, case_id)
        if c is None:
            raise LookupError(case_id)
        added = 0
        for it in items:
            ev = ComplianceCaseEvidence(
                id=uuid.uuid4().hex,
                case_id=c.id,
                resource_type=it["resource_type"],
                resource_id=str(it["resource_id"]),
                tag=it.get("tag") or "relevant",
                notes=it.get("notes"),
                snapshot=it.get("snapshot"),
                added_by=actor_id,
            )
            db.add(ev)
            added += 1
        c.evidence_count = (c.evidence_count or 0) + added
        await db.commit()
        audit_log("compliance.case_evidence_added", user_id=actor_id, success=True,
                  details={"case_id": c.id, "added": added})
        return {"added": added}

    async def case_timeline(
        self, db: AsyncSession, case_id: str, *, limit: int = 500,
    ) -> List[Dict[str, Any]]:
        rows = (await db.execute(
            select(ComplianceCaseEvidence)
            .where(ComplianceCaseEvidence.case_id == case_id)
            .order_by(desc(ComplianceCaseEvidence.added_at))
            .limit(limit)
        )).scalars().all()
        return [{
            "evidence_id": r.id,
            "resource_type": r.resource_type,
            "resource_id": r.resource_id,
            "tag": r.tag,
            "notes": r.notes,
            "added_by": r.added_by,
            "added_at": r.added_at.isoformat() if r.added_at else None,
            "snapshot": r.snapshot,
        } for r in rows]


ediscovery_engine = EDiscoveryEngine()
