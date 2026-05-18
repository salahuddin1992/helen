"""
Call-recording orchestrator with client-triggerable lifecycle.

The mediasoup SFU worker can already produce composite recordings via
ffmpeg (see sfu_events.py). This module exposes that machinery to
the chat clients via REST + Socket.IO so any participant with the
``CAP_RECORD_CALLS`` capability can start/stop a recording.

Flow
----
  1. Client A calls   POST /api/calls/{id}/recording/start
                       — server picks an output path, asks the
                         mediasoup worker to start ffmpeg.
                       — broadcasts ``call:recording_started`` on
                         Socket.IO so every participant sees the red
                         dot in their UI.
  2. Worker writes    {recording_id}.mkv to the configured directory.
  3. Client A calls   POST /api/calls/{id}/recording/stop
                       — worker flushes ffmpeg, computes duration +
                         file size, persists metadata.
                       — broadcasts ``call:recording_stopped``.
  4. Any client can   GET  /api/calls/{id}/recordings
                       to list past recordings.
                      GET  /api/recordings/{rid}/download
                       to stream the file (auth-gated).

Privacy
-------
Recordings are only visible to participants who were in the call
when it started, plus channel admins. Deletion requires either the
recording owner (the client that pressed Start) or an admin.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional


@dataclass
class Recording:
    recording_id: str
    call_id: str
    channel_id: str
    started_by: str
    started_at: float
    stopped_at: Optional[float] = None
    output_path: str = ""
    size_bytes: int = 0
    duration_sec: float = 0.0
    participants: list[str] = field(default_factory=list)
    status: str = "recording"      # recording | stopped | failed | deleted
    error: str = ""


class CallRecordingStore:
    """Lifecycle + metadata persistence for call recordings.

    Wire-up
    -------
      * The Socket.IO room broadcaster must be supplied at init so the
        store can fan ``call:recording_started`` / ``call:recording_stopped``
        out to every participant.
      * The actual ffmpeg invocation lives in the mediasoup worker
        (``sfu-worker/src/recording.js``); this module only sends the
        ``ROUTER_RECORD_START`` / ``ROUTER_RECORD_STOP`` IPC events
        and updates SQLite when the worker reports completion.
    """

    DEFAULT_FORMAT = "mkv"        # ffmpeg-friendly, lossless container

    def __init__(
        self,
        db_path: str,
        recordings_dir: str,
        broadcast_to_call: Optional[
            Callable[[str, str, dict], Awaitable[None]]
        ] = None,
        sfu_send_ipc: Optional[
            Callable[[str, dict], Awaitable[None]]
        ] = None,
    ) -> None:
        self.db_path = db_path
        self.recordings_dir = recordings_dir
        os.makedirs(recordings_dir, exist_ok=True)
        self.broadcast_to_call = broadcast_to_call
        self.sfu_send_ipc = sfu_send_ipc
        self._lock = asyncio.Lock()
        self._init_schema()

    def _init_schema(self) -> None:
        with sqlite3.connect(self.db_path) as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS call_recordings (
                    recording_id    TEXT PRIMARY KEY,
                    call_id         TEXT NOT NULL,
                    channel_id      TEXT NOT NULL,
                    started_by      TEXT NOT NULL,
                    started_at      REAL NOT NULL,
                    stopped_at      REAL,
                    output_path     TEXT NOT NULL,
                    size_bytes      INTEGER DEFAULT 0,
                    duration_sec    REAL DEFAULT 0,
                    participants    TEXT NOT NULL,
                    status          TEXT NOT NULL,
                    error           TEXT
                )
            """)
            c.execute("CREATE INDEX IF NOT EXISTS idx_rec_call "
                       "ON call_recordings(call_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_rec_channel "
                       "ON call_recordings(channel_id)")

    async def start(
        self, call_id: str, channel_id: str, started_by: str,
        participants: list[str],
    ) -> Recording:
        """Start a recording for an active call."""
        async with self._lock:
            rid = secrets.token_urlsafe(12)
            output = os.path.join(
                self.recordings_dir,
                f"{call_id}_{rid}.{self.DEFAULT_FORMAT}",
            )
            rec = Recording(
                recording_id=rid, call_id=call_id, channel_id=channel_id,
                started_by=started_by, started_at=time.time(),
                output_path=output, participants=list(participants),
                status="recording",
            )
            self._persist(rec)

        # Tell the SFU worker to fire up ffmpeg
        if self.sfu_send_ipc:
            await self.sfu_send_ipc("ROUTER_RECORD_START", {
                "call_id": call_id,
                "recording_id": rid,
                "output_path": output,
                "format": self.DEFAULT_FORMAT,
            })

        # Broadcast to participants so the UI can show a red dot
        if self.broadcast_to_call:
            await self.broadcast_to_call(call_id, "call:recording_started", {
                "recording_id": rid,
                "started_by": started_by,
                "started_at": rec.started_at,
            })
        return rec

    async def stop(self, recording_id: str,
                    stopped_by: str) -> Optional[Recording]:
        """Mark a recording as stopped. Worker reports the final
        size/duration via :meth:`worker_finalize`."""
        async with self._lock:
            rec = self._fetch(recording_id)
            if not rec:
                return None
            if rec.status != "recording":
                return rec
            rec.stopped_at = time.time()
            rec.status = "stopped"
            self._persist(rec)

        if self.sfu_send_ipc:
            await self.sfu_send_ipc("ROUTER_RECORD_STOP", {
                "recording_id": recording_id,
            })

        if self.broadcast_to_call:
            await self.broadcast_to_call(
                rec.call_id, "call:recording_stopped",
                {
                    "recording_id": recording_id,
                    "stopped_by": stopped_by,
                    "stopped_at": rec.stopped_at,
                },
            )
        return rec

    async def worker_finalize(
        self, recording_id: str, size_bytes: int,
        duration_sec: float, error: str = "",
    ) -> None:
        """Called by sfu_events when ffmpeg flushes the output."""
        async with self._lock:
            rec = self._fetch(recording_id)
            if not rec:
                return
            rec.size_bytes = size_bytes
            rec.duration_sec = duration_sec
            if error:
                rec.status = "failed"
                rec.error = error
            else:
                rec.status = "stopped"
            self._persist(rec)

        if self.broadcast_to_call:
            await self.broadcast_to_call(
                rec.call_id, "call:recording_ready",
                {
                    "recording_id": recording_id,
                    "size_bytes": size_bytes,
                    "duration_sec": duration_sec,
                    "status": rec.status,
                    "error": rec.error or None,
                },
            )

    async def list_for_call(self, call_id: str) -> list[Recording]:
        return await asyncio.to_thread(self._list_for_call_sync, call_id)

    def _list_for_call_sync(self, call_id: str) -> list[Recording]:
        with sqlite3.connect(self.db_path) as c:
            rows = c.execute(
                "SELECT * FROM call_recordings WHERE call_id=? "
                "ORDER BY started_at DESC", (call_id,),
            ).fetchall()
        return [self._row_to_recording(r) for r in rows]

    async def list_for_channel(self, channel_id: str
                                 ) -> list[Recording]:
        return await asyncio.to_thread(self._list_for_channel_sync,
                                         channel_id)

    def _list_for_channel_sync(self, channel_id: str) -> list[Recording]:
        with sqlite3.connect(self.db_path) as c:
            rows = c.execute(
                "SELECT * FROM call_recordings WHERE channel_id=? "
                "ORDER BY started_at DESC", (channel_id,),
            ).fetchall()
        return [self._row_to_recording(r) for r in rows]

    async def delete(self, recording_id: str,
                      requester: str, is_admin: bool = False
                      ) -> bool:
        async with self._lock:
            rec = self._fetch(recording_id)
            if not rec:
                return False
            if not is_admin and rec.started_by != requester:
                return False
            try:
                if os.path.exists(rec.output_path):
                    os.remove(rec.output_path)
            except OSError:
                pass
            rec.status = "deleted"
            self._persist(rec)
            return True

    # ── internals ────────────────────────────────────────────

    def _persist(self, rec: Recording) -> None:
        with sqlite3.connect(self.db_path) as c:
            c.execute(
                "INSERT OR REPLACE INTO call_recordings VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (rec.recording_id, rec.call_id, rec.channel_id,
                 rec.started_by, rec.started_at, rec.stopped_at,
                 rec.output_path, rec.size_bytes, rec.duration_sec,
                 json.dumps(rec.participants), rec.status,
                 rec.error or None),
            )

    def _fetch(self, recording_id: str) -> Optional[Recording]:
        with sqlite3.connect(self.db_path) as c:
            row = c.execute(
                "SELECT * FROM call_recordings WHERE recording_id=?",
                (recording_id,),
            ).fetchone()
        return self._row_to_recording(row) if row else None

    def _row_to_recording(self, row: tuple) -> Recording:
        return Recording(
            recording_id=row[0], call_id=row[1], channel_id=row[2],
            started_by=row[3], started_at=row[4], stopped_at=row[5],
            output_path=row[6], size_bytes=row[7] or 0,
            duration_sec=row[8] or 0.0,
            participants=json.loads(row[9]) if row[9] else [],
            status=row[10], error=row[11] or "",
        )
