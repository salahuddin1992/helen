"""
Persistent peer trust score — reputation that survives restart.

Each ``server_id`` carries a score in [0.0, 1.0] backed by SQLite so
the cluster doesn't forget a peer's behaviour across restarts:

    1.0  → perfect record, fully trusted
    0.5  → new / unknown peer (default seed)
    0.0  → bad signature, cluster mismatch, or auto-quarantined

Events that move the score:

| Event                  | Effect                          |
| ---------------------- | ------------------------------- |
| successful_exchange    | +0.005 (capped at 1.0)          |
| timeout                | × 0.95                          |
| rate_limit_hit         | × 0.80                          |
| bad_signature          | → 0.0 + 5-min deny              |
| cluster_mismatch       | → 0.0 + sync_policy hard block  |

Storage format
--------------
SQLite table ``peer_trust`` (single file at ``data/trust_db.sqlite``)::

    server_id      TEXT PRIMARY KEY
    score          REAL   NOT NULL DEFAULT 0.5
    success_count  INTEGER NOT NULL DEFAULT 0
    failure_count  INTEGER NOT NULL DEFAULT 0
    violation_count INTEGER NOT NULL DEFAULT 0
    last_event     TEXT
    last_event_at  REAL
    first_seen_at  REAL
    updated_at     REAL

This file is intentionally separate from the main app DB so trust
state survives a full DB rebuild / migration without coupling.

The module is import-light — opens the SQLite handle lazily on first
use so module-level imports (e.g. inside auth gate) don't pay the cost.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

from app.core.logging import get_logger

logger = get_logger(__name__)


_DATA_DIR = Path(os.environ.get("COMMCLIENT_DATA_DIR",
                 str(Path(__file__).resolve().parents[2] / "data")))
_DB_PATH = _DATA_DIR / "trust_db.sqlite"

# Default reputation for a freshly-discovered peer. Optimistic enough
# to give the peer a chance, low enough that one violation can drop
# them well below the auto-quarantine threshold.
_DEFAULT_SCORE = 0.5
_QUARANTINE_THRESHOLD = 0.10


# ── Event → effect mapping ────────────────────────────────────────


def _apply_event(score: float, event: str) -> float:
    if event == "successful_exchange":
        return min(1.0, score + 0.005)
    if event == "timeout":
        return max(0.0, score * 0.95)
    if event == "rate_limit_hit":
        return max(0.0, score * 0.80)
    if event in ("bad_signature", "cluster_mismatch", "replay_detected"):
        return 0.0
    if event == "manual_trust":
        return 1.0
    if event == "manual_distrust":
        return 0.0
    return score  # unknown event → no-op


_VIOLATION_EVENTS = {"bad_signature", "cluster_mismatch", "replay_detected"}


class TrustScoreDB:
    """Singleton SQLite-backed trust store. Thread-safe via a single
    serialized connection (SQLite WAL still allows concurrent reads,
    and the write rate here is tiny)."""

    _singleton: "TrustScoreDB | None" = None

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None

    @classmethod
    def instance(cls) -> "TrustScoreDB":
        if cls._singleton is None:
            cls._singleton = TrustScoreDB()
        return cls._singleton

    # ── Connection management ───────────────────────────────

    def _ensure_conn(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(_DB_PATH),
            check_same_thread=False,
            isolation_level=None,  # autocommit; we batch via transactions
        )
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS peer_trust (
                server_id        TEXT    PRIMARY KEY,
                score            REAL    NOT NULL DEFAULT 0.5,
                success_count    INTEGER NOT NULL DEFAULT 0,
                failure_count    INTEGER NOT NULL DEFAULT 0,
                violation_count  INTEGER NOT NULL DEFAULT 0,
                last_event       TEXT,
                last_event_at    REAL,
                first_seen_at    REAL    NOT NULL,
                updated_at       REAL    NOT NULL
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_peer_trust_score
              ON peer_trust(score)
        """)
        self._conn = conn
        return conn

    # ── Public API ──────────────────────────────────────────

    def get(self, server_id: str) -> dict:
        sid = (server_id or "").strip()
        if not sid:
            return {}
        with self._lock:
            row = self._ensure_conn().execute(
                "SELECT server_id, score, success_count, failure_count, "
                "violation_count, last_event, last_event_at, first_seen_at, "
                "updated_at FROM peer_trust WHERE server_id = ?",
                (sid,),
            ).fetchone()
        if row is None:
            return {
                "server_id": sid,
                "score": _DEFAULT_SCORE,
                "exists": False,
            }
        return {
            "server_id":       row[0],
            "score":           row[1],
            "success_count":   row[2],
            "failure_count":   row[3],
            "violation_count": row[4],
            "last_event":      row[5],
            "last_event_at":   row[6],
            "first_seen_at":   row[7],
            "updated_at":      row[8],
            "exists":          True,
        }

    def get_score(self, server_id: str) -> float:
        return self.get(server_id).get("score", _DEFAULT_SCORE)

    def is_quarantined(self, server_id: str) -> bool:
        return self.get_score(server_id) < _QUARANTINE_THRESHOLD

    def record_event(self, server_id: str, event: str) -> dict:
        """Apply an event to the peer's score and return the new row.

        On violation events also auto-add to the sync_policy blocklist
        so the peer is rejected at the federation gate immediately.
        """
        sid = (server_id or "").strip()
        if not sid:
            return {}

        now = time.time()
        with self._lock:
            conn = self._ensure_conn()
            row = conn.execute(
                "SELECT score, success_count, failure_count, violation_count, "
                "first_seen_at FROM peer_trust WHERE server_id = ?",
                (sid,),
            ).fetchone()
            if row is None:
                score, succ, fail, viol, first_seen = (
                    _DEFAULT_SCORE, 0, 0, 0, now,
                )
            else:
                score, succ, fail, viol, first_seen = row

            new_score = _apply_event(score, event)

            if event == "successful_exchange":
                succ += 1
            elif event in ("timeout", "rate_limit_hit"):
                fail += 1
            elif event in _VIOLATION_EVENTS:
                viol += 1

            conn.execute(
                "INSERT INTO peer_trust (server_id, score, success_count, "
                "failure_count, violation_count, last_event, last_event_at, "
                "first_seen_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(server_id) DO UPDATE SET "
                "score = excluded.score, "
                "success_count = excluded.success_count, "
                "failure_count = excluded.failure_count, "
                "violation_count = excluded.violation_count, "
                "last_event = excluded.last_event, "
                "last_event_at = excluded.last_event_at, "
                "updated_at = excluded.updated_at",
                (sid, new_score, succ, fail, viol, event, now,
                 first_seen, now),
            )

        # Side-effects outside the DB lock to avoid re-entrancy with
        # sync_policy's own lock.
        if event in _VIOLATION_EVENTS or new_score < _QUARANTINE_THRESHOLD:
            try:
                from app.services.sync_policy import get_sync_policy
                get_sync_policy().block(sid)
                logger.warning(
                    "trust_auto_quarantine",
                    server_id=sid[:24], event=event, score=new_score,
                )
            except Exception:
                pass

        return self.get(sid)

    def list_top(self, limit: int = 100, ascending: bool = False) -> list[dict]:
        order = "ASC" if ascending else "DESC"
        with self._lock:
            rows = self._ensure_conn().execute(
                f"SELECT server_id, score, success_count, failure_count, "
                f"violation_count, last_event, last_event_at, first_seen_at, "
                f"updated_at FROM peer_trust ORDER BY score {order} LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [
            {
                "server_id":       r[0],
                "score":           r[1],
                "success_count":   r[2],
                "failure_count":   r[3],
                "violation_count": r[4],
                "last_event":      r[5],
                "last_event_at":   r[6],
                "first_seen_at":   r[7],
                "updated_at":      r[8],
            }
            for r in rows
        ]

    def reset(self, server_id: str) -> dict:
        """Wipe a peer's history (after manual rehabilitation)."""
        sid = (server_id or "").strip()
        if not sid:
            return {}
        with self._lock:
            self._ensure_conn().execute(
                "DELETE FROM peer_trust WHERE server_id = ?", (sid,),
            )
        return {"server_id": sid, "reset": True}


def get_trust_db() -> TrustScoreDB:
    return TrustScoreDB.instance()
