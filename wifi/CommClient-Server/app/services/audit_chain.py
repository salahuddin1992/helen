"""
Tamper-evident audit log — Merkle-style hash chain.

Why
---
Helen records security-sensitive events (logins, role grants,
moderation actions, vault opens). A motivated attacker who got
SQLite-write access could rewrite history. The chain in this module
makes that impossible without breaking the chain — any tampering
shows up as a hash mismatch on the next verification pass.

Wire shape
----------
Every appended record stores three hashes:

    seq          — monotonic 64-bit counter (gap-detection)
    payload_hash — SHA-256 of the JSON payload
    prev_hash    — payload_hash of the previous record
                   (== "GENESIS-helen-audit-v1" for seq=1)
    chain_hash   — SHA-256(prev_hash || payload_hash)

To verify the whole log, walk it in order: each record's
``chain_hash`` must equal SHA-256(record[i-1].chain_hash ||
record[i].payload_hash). A single byte change anywhere in the chain
breaks every subsequent ``chain_hash``.

For inclusion proofs (e.g. "show this single entry to an external
auditor") the caller stores the entry's seq + chain_hash separately;
producing the entry plus the chain_hash of the next entry proves
both inclusion and ordering.

Storage
-------
Append-only SQLite table. We deliberately don't expose UPDATE / DELETE
helpers — the only safe way to remove records is to roll the whole
database forward to a new genesis (a fresh log file).
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional


GENESIS_HASH = hashlib.sha256(b"GENESIS-helen-audit-v1").hexdigest()


def _hash_payload(payload: dict[str, Any]) -> str:
    """SHA-256 over canonical JSON. ``sort_keys`` + ``separators``
    make the bytes deterministic across Python versions and OS endian."""
    blob = json.dumps(
        payload, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _link(prev_hash: str, payload_hash: str) -> str:
    return hashlib.sha256(
        (prev_hash + payload_hash).encode("ascii"),
    ).hexdigest()


@dataclass
class AuditEntry:
    seq: int
    timestamp: float
    actor: str               # who did it (user_id, "system", "router")
    action: str              # short verb — "login", "channel.delete", …
    target: Optional[str]    # the resource the action affected
    payload: dict[str, Any]  # arbitrary structured detail
    payload_hash: str = ""
    prev_hash: str = ""
    chain_hash: str = ""


class AuditChain:
    """Append-only tamper-evident log."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._lock = threading.Lock()
        with sqlite3.connect(db_path) as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS audit_chain (
                    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp    REAL NOT NULL,
                    actor        TEXT NOT NULL,
                    action       TEXT NOT NULL,
                    target       TEXT,
                    payload_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    prev_hash    TEXT NOT NULL,
                    chain_hash   TEXT NOT NULL
                )
            """)
            c.execute("CREATE INDEX IF NOT EXISTS idx_audit_actor "
                       "ON audit_chain(actor)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_audit_action "
                       "ON audit_chain(action)")

    def append(
        self,
        actor: str,
        action: str,
        *,
        target: Optional[str] = None,
        payload: Optional[dict] = None,
    ) -> AuditEntry:
        """Atomically append a new record. The caller doesn't need
        to know the previous hash; we read+write inside a single
        SQLite transaction holding the chain lock."""
        payload = dict(payload or {})
        # Normalise — never let caller-supplied keys collide with
        # our reserved field names.
        for reserved in ("seq", "payload_hash", "prev_hash", "chain_hash"):
            payload.pop(reserved, None)

        ts = time.time()
        with self._lock, sqlite3.connect(self.db_path) as c:
            row = c.execute(
                "SELECT chain_hash FROM audit_chain "
                "ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            prev_hash = row[0] if row else GENESIS_HASH

            payload_hash = _hash_payload({
                "ts": ts, "actor": actor,
                "action": action, "target": target,
                "payload": payload,
            })
            chain_hash = _link(prev_hash, payload_hash)

            cur = c.execute(
                "INSERT INTO audit_chain "
                "(timestamp, actor, action, target, payload_json, "
                " payload_hash, prev_hash, chain_hash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (ts, actor, action, target,
                 json.dumps(payload, ensure_ascii=False),
                 payload_hash, prev_hash, chain_hash),
            )
            seq = cur.lastrowid

        return AuditEntry(
            seq=seq, timestamp=ts, actor=actor, action=action,
            target=target, payload=payload, payload_hash=payload_hash,
            prev_hash=prev_hash, chain_hash=chain_hash,
        )

    def verify(self) -> tuple[bool, Optional[int], str]:
        """Walk the entire chain, return ``(ok, broken_at_seq, msg)``.

        Cheap: ~50 µs per record on a modern CPU. Run it on a timer
        (every 5 minutes) and alert on failure — that's how you turn
        a hash chain into a real-time tamper alarm.
        """
        with sqlite3.connect(self.db_path) as c:
            cur = c.execute(
                "SELECT seq, timestamp, actor, action, target, "
                "payload_json, payload_hash, prev_hash, chain_hash "
                "FROM audit_chain ORDER BY seq ASC"
            )
            expected_prev = GENESIS_HASH
            last_seq = 0
            for row in cur:
                (seq, ts, actor, action, target, payload_json,
                 stored_payload_hash, stored_prev_hash,
                 stored_chain_hash) = row

                # 1) Sequential
                if seq != last_seq + 1:
                    return (
                        False, seq,
                        f"sequence gap at seq={seq} "
                        f"(expected {last_seq + 1})",
                    )
                last_seq = seq

                # 2) Stored prev_hash matches our walking pointer
                if stored_prev_hash != expected_prev:
                    return (
                        False, seq,
                        f"prev_hash mismatch at seq={seq}: "
                        f"row says {stored_prev_hash[:12]}…, "
                        f"expected {expected_prev[:12]}…",
                    )

                # 3) Re-hash payload, compare
                computed_payload_hash = _hash_payload({
                    "ts": ts, "actor": actor, "action": action,
                    "target": target,
                    "payload": json.loads(payload_json),
                })
                if computed_payload_hash != stored_payload_hash:
                    return (
                        False, seq,
                        f"payload_hash mismatch at seq={seq}: "
                        f"someone edited the payload",
                    )

                # 4) Re-link, compare
                computed_chain = _link(expected_prev, stored_payload_hash)
                if computed_chain != stored_chain_hash:
                    return (
                        False, seq,
                        f"chain_hash mismatch at seq={seq}",
                    )

                expected_prev = stored_chain_hash

        return True, None, "chain_intact"

    def head(self) -> Optional[AuditEntry]:
        """Latest record — useful for periodic externalisation
        (e.g. "publish today's chain head to an off-site signpost")."""
        with sqlite3.connect(self.db_path) as c:
            row = c.execute(
                "SELECT seq, timestamp, actor, action, target, "
                "payload_json, payload_hash, prev_hash, chain_hash "
                "FROM audit_chain ORDER BY seq DESC LIMIT 1"
            ).fetchone()
        if not row:
            return None
        return AuditEntry(
            seq=row[0], timestamp=row[1], actor=row[2], action=row[3],
            target=row[4], payload=json.loads(row[5]),
            payload_hash=row[6], prev_hash=row[7], chain_hash=row[8],
        )

    def filter(
        self,
        *,
        actor: Optional[str] = None,
        action: Optional[str] = None,
        since: Optional[float] = None,
        limit: int = 1000,
    ) -> Iterator[AuditEntry]:
        sql = (
            "SELECT seq, timestamp, actor, action, target, "
            "payload_json, payload_hash, prev_hash, chain_hash "
            "FROM audit_chain WHERE 1=1"
        )
        params: list = []
        if actor:
            sql += " AND actor = ?"
            params.append(actor)
        if action:
            sql += " AND action = ?"
            params.append(action)
        if since:
            sql += " AND timestamp >= ?"
            params.append(since)
        sql += " ORDER BY seq DESC LIMIT ?"
        params.append(limit)

        with sqlite3.connect(self.db_path) as c:
            for row in c.execute(sql, params):
                yield AuditEntry(
                    seq=row[0], timestamp=row[1], actor=row[2],
                    action=row[3], target=row[4],
                    payload=json.loads(row[5]),
                    payload_hash=row[6], prev_hash=row[7],
                    chain_hash=row[8],
                )


# ── Module-level singleton ──────────────────────────────────────────


_CHAIN: Optional[AuditChain] = None
_CHAIN_LOCK = threading.Lock()


def configure_audit_chain(db_path: str) -> AuditChain:
    """Initialise the process-wide chain. Idempotent."""
    global _CHAIN
    with _CHAIN_LOCK:
        if _CHAIN is None:
            os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
            _CHAIN = AuditChain(db_path)
    return _CHAIN


def get_audit_chain() -> Optional[AuditChain]:
    return _CHAIN
