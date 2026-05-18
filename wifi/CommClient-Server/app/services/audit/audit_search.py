"""
Re-export of the legacy ``app.services.audit_search`` module under the
``app.services.audit`` namespace. Provides additional SQLite-backed
filtering against the AuditChain database (the JSONL log is legacy).
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from app.services.audit_chain import get_audit_chain
from app.services.audit_search import (  # re-export legacy callable
    event_counts,
    search,
)


def _ro_connect(db_path: str) -> sqlite3.Connection:
    uri = f"file:{Path(db_path).as_posix()}?mode=ro"
    c = sqlite3.connect(uri, uri=True, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def _severity_of(action: str, payload: dict[str, Any]) -> str:
    """Derive a severity bucket from action+payload. The legacy chain
    didn't carry an explicit severity column — we infer one for the
    dashboard so filters work."""
    if payload.get("severity"):
        return str(payload["severity"]).lower()
    if not action:
        return "info"
    a = action.lower()
    if any(k in a for k in ("tamper", "denied", "unauthorized", "locked", "rbac_denied")):
        return "critical"
    if any(k in a for k in ("delete", "ban", "kick", "revoke", "purge")):
        return "high"
    if any(k in a for k in ("failed", "error", "rate_limited")):
        return "medium"
    if any(k in a for k in ("login", "logout", "token", "permission")):
        return "low"
    return "info"


def query_entries(
    *,
    cursor: Optional[int] = None,
    limit: int = 100,
    from_ts: Optional[float] = None,
    to_ts: Optional[float] = None,
    actor: Optional[str] = None,
    action: Optional[str] = None,
    resource: Optional[str] = None,
    severity: Optional[str] = None,
    q: Optional[str] = None,
) -> dict[str, Any]:
    """Cursor-paginated, multi-filter entry query against the audit
    chain SQLite. ``cursor`` is the ``seq`` to paginate *before* (we
    return entries with ``seq < cursor`` in descending order)."""
    chain = get_audit_chain()
    if chain is None:
        return {"entries": [], "next_cursor": None, "total": 0}

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
    if resource:
        sql += " AND target = ?"
        params.append(resource)
    if from_ts is not None:
        sql += " AND timestamp >= ?"
        params.append(from_ts)
    if to_ts is not None:
        sql += " AND timestamp <= ?"
        params.append(to_ts)
    if q:
        sql += " AND (payload_json LIKE ? OR actor LIKE ? OR action LIKE ? OR target LIKE ?)"
        like = f"%{q}%"
        params.extend([like, like, like, like])

    sql += " ORDER BY seq DESC LIMIT ?"
    # We over-fetch and filter by severity in-memory since severity is derived.
    fetch = limit * 5 if severity else limit
    params.append(fetch)

    entries: list[dict[str, Any]] = []
    total = 0
    with _ro_connect(chain.db_path) as c:
        total_row = c.execute("SELECT COUNT(1) AS n FROM audit_chain").fetchone()
        total = int(total_row["n"]) if total_row else 0

        for row in c.execute(sql, params):
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except Exception:
                payload = {}
            sev = _severity_of(row["action"], payload)
            if severity and sev != severity.lower():
                continue
            entries.append({
                "seq": row["seq"],
                "timestamp": row["timestamp"],
                "actor": row["actor"],
                "action": row["action"],
                "resource": row["target"],
                "severity": sev,
                "payload": payload,
                "payload_hash": row["payload_hash"],
                "prev_hash": row["prev_hash"],
                "chain_hash": row["chain_hash"],
            })
            if len(entries) >= limit:
                break

    next_cursor = entries[-1]["seq"] if entries and len(entries) == limit else None
    return {"entries": entries, "next_cursor": next_cursor, "total": total}


def stats(*, since_24h: bool = True, since_7d: bool = True) -> dict[str, Any]:
    chain = get_audit_chain()
    if chain is None:
        return {
            "total": 0, "entries_24h": 0, "entries_7d": 0,
            "by_severity": {}, "by_action": {},
            "by_actor_top10": [], "by_resource_top10": [],
        }
    now = time.time()
    cutoff_24h = now - 24 * 3600
    cutoff_7d = now - 7 * 86400

    by_severity: dict[str, int] = {}
    by_action: dict[str, int] = {}
    by_actor: dict[str, int] = {}
    by_resource: dict[str, int] = {}
    total = 0
    n_24h = 0
    n_7d = 0

    with _ro_connect(chain.db_path) as c:
        for row in c.execute(
            "SELECT timestamp, actor, action, target, payload_json FROM audit_chain"
        ):
            total += 1
            ts = float(row["timestamp"] or 0)
            if ts >= cutoff_24h:
                n_24h += 1
            if ts >= cutoff_7d:
                n_7d += 1
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except Exception:
                payload = {}
            sev = _severity_of(row["action"], payload)
            by_severity[sev] = by_severity.get(sev, 0) + 1
            by_action[row["action"]] = by_action.get(row["action"], 0) + 1
            by_actor[row["actor"]] = by_actor.get(row["actor"], 0) + 1
            if row["target"]:
                by_resource[row["target"]] = by_resource.get(row["target"], 0) + 1

    def _top(d: dict[str, int], n: int) -> list[dict[str, Any]]:
        return [{"key": k, "count": v}
                for k, v in sorted(d.items(), key=lambda kv: kv[1], reverse=True)[:n]]

    return {
        "total": total,
        "entries_24h": n_24h,
        "entries_7d": n_7d,
        "by_severity": by_severity,
        "by_action": dict(sorted(by_action.items(), key=lambda kv: kv[1], reverse=True)[:50]),
        "by_actor_top10": _top(by_actor, 10),
        "by_resource_top10": _top(by_resource, 10),
        "as_of": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
    }


def suggest_actors(prefix: str, *, limit: int = 20) -> list[str]:
    chain = get_audit_chain()
    if chain is None or not prefix:
        return []
    with _ro_connect(chain.db_path) as c:
        rows = c.execute(
            "SELECT DISTINCT actor FROM audit_chain "
            "WHERE actor LIKE ? ORDER BY actor ASC LIMIT ?",
            (f"{prefix}%", limit),
        ).fetchall()
    return [r["actor"] for r in rows if r["actor"]]


__all__ = [
    "query_entries",
    "stats",
    "suggest_actors",
    "search",
    "event_counts",
]
