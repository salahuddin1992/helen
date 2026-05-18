"""
Communication-layer features missing from v1: presence, read
receipts, typing indicators, voice messages, full-text search.

Each feature is implemented as a small, self-contained class so the
existing chat / call routes can opt in by adding one method call.
None of these depend on each other.

  PresenceTracker        — online / offline / away / busy / DND state
  ReadReceiptStore       — last-read message id per (user, channel)
  TypingTracker          — short-lived typing flags
  VoiceMessageStore      — opus/webm uploads + duration metadata
  MessageSearchIndex     — SQLite FTS5 full-text index
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Iterable, Optional


# ── Presence ────────────────────────────────────────────────────────


@dataclass
class PresenceState:
    user_id: str
    status: str = "offline"     # online | away | busy | dnd | offline
    custom_message: str = ""
    last_seen: float = field(default_factory=time.time)
    devices: set[str] = field(default_factory=set)


class PresenceTracker:
    """Async-safe in-memory presence map.

    Wire it up by calling ``mark_online(user_id, device_id)`` from
    the Socket.IO connect handler and ``mark_offline()`` from the
    disconnect handler. Status broadcasts go via the existing
    Socket.IO room machinery — this class only exposes the data.
    """

    AUTO_AWAY_AFTER_SEC = 5 * 60  # 5 min idle → "away"

    def __init__(self) -> None:
        self._states: dict[str, PresenceState] = {}
        self._lock = asyncio.Lock()

    async def mark_online(self, user_id: str, device_id: str) -> None:
        async with self._lock:
            st = self._states.setdefault(user_id, PresenceState(user_id))
            st.status = "online"
            st.devices.add(device_id)
            st.last_seen = time.time()

    async def mark_offline(self, user_id: str,
                            device_id: Optional[str] = None) -> None:
        async with self._lock:
            st = self._states.get(user_id)
            if not st:
                return
            if device_id:
                st.devices.discard(device_id)
            else:
                st.devices.clear()
            if not st.devices:
                st.status = "offline"
            st.last_seen = time.time()

    async def set_status(self, user_id: str, status: str,
                          message: str = "") -> None:
        if status not in ("online", "away", "busy", "dnd", "offline"):
            return
        async with self._lock:
            st = self._states.setdefault(user_id, PresenceState(user_id))
            st.status = status
            st.custom_message = message
            st.last_seen = time.time()

    async def get(self, user_id: str) -> PresenceState:
        async with self._lock:
            st = self._states.get(user_id)
            if st is None:
                return PresenceState(user_id=user_id, status="offline")
            # Auto-away promotion
            if (st.status == "online"
                    and time.time() - st.last_seen
                    > self.AUTO_AWAY_AFTER_SEC):
                st.status = "away"
            return st

    async def all_online(self) -> list[str]:
        async with self._lock:
            return [
                uid for uid, st in self._states.items()
                if st.status in ("online", "away", "busy")
            ]


# ── Read receipts ───────────────────────────────────────────────────


class ReadReceiptStore:
    """Persists last-read message id per (user, channel) in SQLite.

    Cheap enough to call inline from the message-fetch handler — one
    INSERT OR REPLACE per fetch. Returns the per-channel "high water
    mark" so each device can render unread badges.
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _init_schema(self) -> None:
        with self._conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS read_receipts (
                    user_id    TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    last_read  TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (user_id, channel_id)
                )
            """)
            c.execute("""
                CREATE INDEX IF NOT EXISTS idx_rr_channel
                ON read_receipts(channel_id)
            """)

    def mark_read(self, user_id: str, channel_id: str,
                   message_id: str) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO read_receipts VALUES (?, ?, ?, ?)",
                (user_id, channel_id, message_id, time.time()),
            )

    def get_last_read(self, user_id: str,
                       channel_id: str) -> Optional[str]:
        with self._conn() as c:
            r = c.execute(
                "SELECT last_read FROM read_receipts "
                "WHERE user_id=? AND channel_id=?",
                (user_id, channel_id),
            ).fetchone()
            return r[0] if r else None

    def all_for_channel(self, channel_id: str) -> dict[str, str]:
        with self._conn() as c:
            return {
                row[0]: row[1]
                for row in c.execute(
                    "SELECT user_id, last_read FROM read_receipts "
                    "WHERE channel_id=?", (channel_id,))
            }


# ── Typing indicators ───────────────────────────────────────────────


class TypingTracker:
    """In-memory short-lived ``user is typing in channel X`` flags.

    Each ``typing()`` call extends the flag for ``ttl_sec`` seconds.
    A background sweeper expires entries older than the TTL.
    """

    DEFAULT_TTL_SEC = 8.0

    def __init__(self, ttl_sec: float = DEFAULT_TTL_SEC) -> None:
        self.ttl_sec = ttl_sec
        # (channel_id, user_id) → expiry timestamp
        self._typing: dict[tuple[str, str], float] = {}
        self._lock = asyncio.Lock()

    async def typing(self, channel_id: str, user_id: str) -> None:
        async with self._lock:
            self._typing[(channel_id, user_id)] = time.time() + self.ttl_sec

    async def stop(self, channel_id: str, user_id: str) -> None:
        async with self._lock:
            self._typing.pop((channel_id, user_id), None)

    async def channel(self, channel_id: str) -> list[str]:
        now = time.time()
        async with self._lock:
            expired = [k for k, t in self._typing.items() if t < now]
            for k in expired:
                self._typing.pop(k, None)
            return [u for (c, u), t in self._typing.items()
                    if c == channel_id and t >= now]


# ── Voice messages ──────────────────────────────────────────────────


@dataclass
class VoiceMessageMeta:
    message_id: str
    sender_id: str
    channel_id: str
    duration_sec: float
    mime: str
    size_bytes: int
    waveform: list[int] = field(default_factory=list)   # peak amplitudes 0-255
    created_at: float = field(default_factory=time.time)


class VoiceMessageStore:
    """Stores opus/webm uploads in a directory, metadata in SQLite.

    Doesn't transcode (clients stream raw Opus to keep CPU off the
    server). The waveform array is a 32-element peak summary the
    client renders as a chat-bubble visualizer.
    """

    def __init__(self, root_dir: str) -> None:
        self.root_dir = root_dir
        os.makedirs(root_dir, exist_ok=True)
        self._db = os.path.join(root_dir, "voice_meta.sqlite")
        with sqlite3.connect(self._db) as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS voice_messages (
                    message_id   TEXT PRIMARY KEY,
                    sender_id    TEXT NOT NULL,
                    channel_id   TEXT NOT NULL,
                    duration_sec REAL NOT NULL,
                    mime         TEXT NOT NULL,
                    size_bytes   INTEGER NOT NULL,
                    waveform     TEXT NOT NULL,
                    created_at   REAL NOT NULL
                )
            """)
            c.execute("CREATE INDEX IF NOT EXISTS idx_vm_chan "
                       "ON voice_messages(channel_id)")

    def store(self, meta: VoiceMessageMeta, audio_bytes: bytes) -> str:
        path = os.path.join(self.root_dir, f"{meta.message_id}.opus")
        with open(path, "wb") as f:
            f.write(audio_bytes)
        with sqlite3.connect(self._db) as c:
            c.execute(
                "INSERT OR REPLACE INTO voice_messages VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?)",
                (meta.message_id, meta.sender_id, meta.channel_id,
                 meta.duration_sec, meta.mime, meta.size_bytes,
                 json.dumps(meta.waveform), meta.created_at),
            )
        return path

    def fetch(self, message_id: str) -> Optional[
            tuple[VoiceMessageMeta, bytes]]:
        with sqlite3.connect(self._db) as c:
            row = c.execute(
                "SELECT sender_id, channel_id, duration_sec, mime, "
                "size_bytes, waveform, created_at "
                "FROM voice_messages WHERE message_id=?",
                (message_id,),
            ).fetchone()
        if not row:
            return None
        meta = VoiceMessageMeta(
            message_id=message_id,
            sender_id=row[0], channel_id=row[1],
            duration_sec=row[2], mime=row[3], size_bytes=row[4],
            waveform=json.loads(row[5]), created_at=row[6],
        )
        path = os.path.join(self.root_dir, f"{message_id}.opus")
        try:
            with open(path, "rb") as f:
                return meta, f.read()
        except FileNotFoundError:
            return None


# ── Full-text search ────────────────────────────────────────────────


class MessageSearchIndex:
    """SQLite FTS5 index over message bodies.

    Indexed fields: ``content``, ``sender_id``, ``channel_id``.
    Use ``rebuild_from_messages()`` once on first boot, then call
    ``add()`` from the message-create hook for incremental updates.
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        with sqlite3.connect(db_path) as c:
            c.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
                USING fts5(
                    message_id, sender_id, channel_id, content,
                    tokenize='unicode61 remove_diacritics 2'
                )
            """)

    def add(self, message_id: str, sender_id: str, channel_id: str,
             content: str) -> None:
        with sqlite3.connect(self.db_path) as c:
            c.execute(
                "INSERT INTO messages_fts(message_id, sender_id, "
                "channel_id, content) VALUES (?, ?, ?, ?)",
                (message_id, sender_id, channel_id, content),
            )

    def search(self, query: str, channel_id: Optional[str] = None,
                limit: int = 50) -> list[dict]:
        with sqlite3.connect(self.db_path) as c:
            if channel_id:
                rows = c.execute("""
                    SELECT message_id, sender_id, channel_id,
                           snippet(messages_fts, 3, '<b>', '</b>', '…', 12)
                    FROM messages_fts
                    WHERE messages_fts MATCH ? AND channel_id=?
                    ORDER BY rank LIMIT ?
                """, (query, channel_id, limit)).fetchall()
            else:
                rows = c.execute("""
                    SELECT message_id, sender_id, channel_id,
                           snippet(messages_fts, 3, '<b>', '</b>', '…', 12)
                    FROM messages_fts
                    WHERE messages_fts MATCH ?
                    ORDER BY rank LIMIT ?
                """, (query, limit)).fetchall()
        return [
            {"message_id": r[0], "sender_id": r[1],
             "channel_id": r[2], "snippet": r[3]}
            for r in rows
        ]

    def remove(self, message_id: str) -> None:
        with sqlite3.connect(self.db_path) as c:
            c.execute("DELETE FROM messages_fts WHERE message_id=?",
                       (message_id,))

    def rebuild_from_messages(
        self, source_iter: Iterable[tuple[str, str, str, str]],
    ) -> int:
        """Bulk-load the FTS index from an iterator of
        ``(message_id, sender_id, channel_id, content)``.
        Returns count loaded."""
        n = 0
        with sqlite3.connect(self.db_path) as c:
            c.execute("DELETE FROM messages_fts")
            for mid, sid, cid, content in source_iter:
                c.execute(
                    "INSERT INTO messages_fts(message_id, sender_id, "
                    "channel_id, content) VALUES (?, ?, ?, ?)",
                    (mid, sid, cid, content),
                )
                n += 1
        return n
