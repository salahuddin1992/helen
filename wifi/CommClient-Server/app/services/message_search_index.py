"""Message search index — SQLite FTS5 full-text search.

A separate index file (``data/message_index.sqlite``) holds an FTS5
virtual table that mirrors message bodies + room_id + sender. This
keeps the main database fast for write-heavy chat workloads while
giving operators a sub-second search across history.

Public API:

  * ``index(message_id, room_id, sender_id, body, ts)``
  * ``search(query, room_id=None, limit=50)``
  * ``delete(message_id)``
  * ``stats()``

The index is best-effort: if FTS5 is missing in the SQLite build
(rare on modern Python), all calls return empty/no-op without
breaking the message flow.
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
_DB_PATH  = _DATA_DIR / "message_index.sqlite"


def _fts5_available(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS _fts5_probe "
            "USING fts5(x);"
        )
        conn.execute("DROP TABLE IF EXISTS _fts5_probe")
        return True
    except sqlite3.OperationalError:
        return False


class MessageSearchIndex:
    _singleton: "MessageSearchIndex | None" = None

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        self._fts_ok = False

    @classmethod
    def instance(cls) -> "MessageSearchIndex":
        if cls._singleton is None:
            cls._singleton = MessageSearchIndex()
        return cls._singleton

    def _ensure_conn(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(
            str(_DB_PATH), check_same_thread=False, isolation_level=None,
        )
        c.execute("PRAGMA journal_mode = WAL")
        c.execute("PRAGMA synchronous = NORMAL")
        if _fts5_available(c):
            self._fts_ok = True
            c.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
                    message_id   UNINDEXED,
                    room_id      UNINDEXED,
                    sender_id    UNINDEXED,
                    body,
                    ts           UNINDEXED,
                    tokenize='porter unicode61'
                )
            """)
        else:
            logger.warning("message_search_fts5_unavailable")
            # Fallback: plain table with LIKE-based search.
            c.execute("""
                CREATE TABLE IF NOT EXISTS messages_fts (
                    message_id   TEXT PRIMARY KEY,
                    room_id      TEXT,
                    sender_id    TEXT,
                    body         TEXT,
                    ts           REAL
                )
            """)
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_fts_room "
                "ON messages_fts(room_id, ts)"
            )
        self._conn = c
        return c

    # ── Mutations ────────────────────────────────────────

    def index(self, message_id: str, room_id: str,
              sender_id: str, body: str,
              *, ts: float | None = None) -> None:
        if not message_id or not body:
            return
        ts = ts if ts is not None else time.time()
        try:
            with self._lock:
                conn = self._ensure_conn()
                if self._fts_ok:
                    # FTS5 supports REPLACE via DELETE+INSERT semantics.
                    conn.execute(
                        "DELETE FROM messages_fts WHERE message_id = ?",
                        (message_id,),
                    )
                    conn.execute(
                        "INSERT INTO messages_fts "
                        "(message_id, room_id, sender_id, body, ts) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (message_id, room_id, sender_id, body, ts),
                    )
                else:
                    conn.execute(
                        "INSERT OR REPLACE INTO messages_fts "
                        "(message_id, room_id, sender_id, body, ts) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (message_id, room_id, sender_id, body, ts),
                    )
        except Exception as e:
            logger.debug("message_index_failed", error=str(e)[:80])

    def delete(self, message_id: str) -> None:
        try:
            with self._lock:
                self._ensure_conn().execute(
                    "DELETE FROM messages_fts WHERE message_id = ?",
                    (message_id,),
                )
        except Exception:
            pass

    # ── Search ──────────────────────────────────────────

    def search(self, query: str,
               *, room_id: str | None = None,
               sender_id: str | None = None,
               limit: int = 50) -> list[dict]:
        if not query or not query.strip():
            return []
        results: list[dict] = []
        try:
            with self._lock:
                conn = self._ensure_conn()
                if self._fts_ok:
                    sql = (
                        "SELECT message_id, room_id, sender_id, body, ts, "
                        "snippet(messages_fts, 3, '<b>', '</b>', '...', 16) "
                        "FROM messages_fts WHERE messages_fts MATCH ? "
                    )
                    params: list = [query]
                    if room_id:
                        sql += "AND room_id = ? "
                        params.append(room_id)
                    if sender_id:
                        sql += "AND sender_id = ? "
                        params.append(sender_id)
                    sql += "ORDER BY rank LIMIT ?"
                    params.append(int(limit))
                    rows = conn.execute(sql, params).fetchall()
                    for r in rows:
                        results.append({
                            "message_id": r[0],
                            "room_id":    r[1],
                            "sender_id":  r[2],
                            "body":       r[3],
                            "ts":         r[4],
                            "snippet":    r[5],
                        })
                else:
                    pattern = f"%{query}%"
                    sql = (
                        "SELECT message_id, room_id, sender_id, body, ts "
                        "FROM messages_fts WHERE body LIKE ? "
                    )
                    params = [pattern]
                    if room_id:
                        sql += "AND room_id = ? "
                        params.append(room_id)
                    if sender_id:
                        sql += "AND sender_id = ? "
                        params.append(sender_id)
                    sql += "ORDER BY ts DESC LIMIT ?"
                    params.append(int(limit))
                    rows = conn.execute(sql, params).fetchall()
                    for r in rows:
                        results.append({
                            "message_id": r[0],
                            "room_id":    r[1],
                            "sender_id":  r[2],
                            "body":       r[3],
                            "ts":         r[4],
                            "snippet":    r[3][:200],
                        })
        except Exception as e:
            logger.debug("message_search_failed", error=str(e)[:80])
        return results

    def stats(self) -> dict:
        try:
            with self._lock:
                row = self._ensure_conn().execute(
                    "SELECT COUNT(*) FROM messages_fts"
                ).fetchone()
            return {
                "fts5":     self._fts_ok,
                "indexed":  int(row[0]) if row else 0,
                "db_path":  str(_DB_PATH),
            }
        except Exception as e:
            return {"error": str(e)[:80]}


def get_message_search() -> MessageSearchIndex:
    return MessageSearchIndex.instance()
