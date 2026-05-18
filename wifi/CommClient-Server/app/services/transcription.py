"""
Local voice transcription via whisper.cpp.

Why whisper.cpp
---------------
- Runs entirely on the host CPU/GPU; no network calls.
- Single self-contained binary; no Python deps to manage in
  PyInstaller.
- Models range from tiny (39 MB) to large-v3 (~3 GB) — admins pick
  the speed/accuracy tradeoff.
- 90+ language detection out of the box, including Arabic.

How Helen wires it
------------------
1. Caller hands us an audio file (.opus / .webm / .wav / .ogg).
2. We hand the path to ``whisper-cli`` (or the older ``main`` bin).
3. JSON output is parsed, segments stored in SQLite per voice
   message / call recording.
4. The transcript becomes searchable via the existing
   MessageSearchIndex (FTS5) + visible in the message bubble.

Configuration
-------------
  HELEN_WHISPER_BIN        path to whisper-cli (or "main")
  HELEN_WHISPER_MODEL      path to .bin model file
  HELEN_WHISPER_LANG       "auto" or ISO code; default "auto"
  HELEN_WHISPER_THREADS    CPU threads (default = ncpu - 1)
  HELEN_WHISPER_FORMAT     output format (default "json")
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class TranscriptSegment:
    start_sec: float
    end_sec: float
    text: str
    language: Optional[str] = None
    confidence: float = 1.0


@dataclass
class Transcript:
    source_id: str               # e.g. message_id or recording_id
    source_kind: str             # "voice_message" | "call_recording"
    language: str = "auto"
    full_text: str = ""
    segments: list[TranscriptSegment] = field(default_factory=list)
    duration_sec: float = 0.0
    transcribed_at: float = field(default_factory=time.time)
    model: str = ""
    elapsed_ms: float = 0.0


class TranscriptionError(RuntimeError):
    pass


class WhisperTranscriber:
    """Thin async wrapper around whisper-cli.

    The whisper binary is forked once per file. Long files (>10 min)
    may take minutes to transcribe even on GPU; treat this as a
    background-only job — never await it inside a request handler.
    """

    def __init__(
        self,
        whisper_bin: Optional[str] = None,
        model_path: Optional[str] = None,
        language: str = "auto",
        threads: Optional[int] = None,
    ) -> None:
        self.whisper_bin = (
            whisper_bin
            or os.environ.get("HELEN_WHISPER_BIN")
            or shutil.which("whisper-cli")
            or shutil.which("main")
        )
        self.model_path = (
            model_path
            or os.environ.get("HELEN_WHISPER_MODEL", "")
        )
        self.language = language or os.environ.get(
            "HELEN_WHISPER_LANG", "auto",
        )
        self.threads = threads or int(
            os.environ.get("HELEN_WHISPER_THREADS",
                            str(max(1, (os.cpu_count() or 2) - 1))),
        )

    def is_available(self) -> bool:
        return bool(self.whisper_bin and self.model_path
                     and Path(self.whisper_bin).exists()
                     and Path(self.model_path).exists())

    async def transcribe(
        self,
        audio_path: str,
        *,
        source_id: str,
        source_kind: str = "voice_message",
        max_seconds_wait: int = 600,
    ) -> Transcript:
        if not self.is_available():
            raise TranscriptionError(
                "whisper.cpp not configured: set HELEN_WHISPER_BIN + "
                "HELEN_WHISPER_MODEL env vars or pass paths to "
                "WhisperTranscriber()."
            )
        if not Path(audio_path).exists():
            raise TranscriptionError(f"audio file not found: {audio_path}")

        with tempfile.TemporaryDirectory() as tmp:
            output_prefix = Path(tmp) / "transcript"
            cmd = [
                self.whisper_bin,
                "-m", self.model_path,
                "-f", audio_path,
                "-of", str(output_prefix),
                "-oj",                  # output JSON
                "-l", self.language,
                "-t", str(self.threads),
                "-pp",                   # print progress on stderr
            ]
            t0 = time.perf_counter()
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    _, stderr = await asyncio.wait_for(
                        proc.communicate(), timeout=max_seconds_wait,
                    )
                except asyncio.TimeoutError:
                    proc.kill()
                    raise TranscriptionError(
                        f"whisper timed out after {max_seconds_wait}s",
                    )
            except FileNotFoundError as exc:
                raise TranscriptionError(
                    f"whisper bin not executable: {exc}",
                )

            elapsed_ms = (time.perf_counter() - t0) * 1000
            if proc.returncode != 0:
                raise TranscriptionError(
                    f"whisper exited {proc.returncode}: "
                    f"{stderr.decode('utf-8', 'replace')[:500]}",
                )

            json_path = output_prefix.with_suffix(".json")
            if not json_path.exists():
                raise TranscriptionError(
                    "whisper produced no JSON output",
                )
            data = json.loads(json_path.read_text(encoding="utf-8"))
            return _parse_whisper_output(
                data, source_id=source_id, source_kind=source_kind,
                elapsed_ms=elapsed_ms,
                model=Path(self.model_path).name,
            )


def _parse_whisper_output(
    data: dict, *, source_id: str, source_kind: str,
    elapsed_ms: float, model: str,
) -> Transcript:
    segments: list[TranscriptSegment] = []
    full = []
    detected_lang = data.get("result", {}).get("language") \
                    or data.get("language") \
                    or "auto"

    transcription = data.get("transcription") or data.get("segments") or []
    for seg in transcription:
        try:
            start = float(seg.get("offsets", {}).get("from", 0)) / 1000
            end = float(seg.get("offsets", {}).get("to", 0)) / 1000
            text = (seg.get("text") or "").strip()
        except Exception:
            start = float(seg.get("start", 0))
            end = float(seg.get("end", 0))
            text = (seg.get("text") or "").strip()
        if not text:
            continue
        segments.append(TranscriptSegment(
            start_sec=start, end_sec=end, text=text,
            language=detected_lang,
        ))
        full.append(text)

    return Transcript(
        source_id=source_id,
        source_kind=source_kind,
        language=detected_lang,
        full_text=" ".join(full),
        segments=segments,
        duration_sec=segments[-1].end_sec if segments else 0.0,
        elapsed_ms=elapsed_ms,
        model=model,
    )


# ── Storage ────────────────────────────────────────────────────────


class TranscriptStore:
    """SQLite store keyed by ``(source_kind, source_id)``."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        with sqlite3.connect(db_path) as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS transcripts (
                    source_id      TEXT NOT NULL,
                    source_kind    TEXT NOT NULL,
                    language       TEXT,
                    full_text      TEXT NOT NULL,
                    segments_json  TEXT NOT NULL,
                    duration_sec   REAL,
                    transcribed_at REAL NOT NULL,
                    model          TEXT,
                    elapsed_ms     REAL,
                    PRIMARY KEY (source_kind, source_id)
                )
            """)
            c.execute("CREATE INDEX IF NOT EXISTS idx_tr_lang "
                       "ON transcripts(language)")

    def save(self, t: Transcript) -> None:
        with sqlite3.connect(self.db_path) as c:
            c.execute(
                "INSERT OR REPLACE INTO transcripts VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    t.source_id, t.source_kind, t.language,
                    t.full_text,
                    json.dumps([s.__dict__ for s in t.segments],
                                ensure_ascii=False),
                    t.duration_sec, t.transcribed_at,
                    t.model, t.elapsed_ms,
                ),
            )

    def get(self, source_kind: str, source_id: str) -> Optional[Transcript]:
        with sqlite3.connect(self.db_path) as c:
            row = c.execute(
                "SELECT source_id, source_kind, language, full_text, "
                "segments_json, duration_sec, transcribed_at, "
                "model, elapsed_ms "
                "FROM transcripts WHERE source_kind=? AND source_id=?",
                (source_kind, source_id),
            ).fetchone()
        if not row:
            return None
        segs = []
        try:
            for d in json.loads(row[4]):
                segs.append(TranscriptSegment(**d))
        except Exception:
            pass
        return Transcript(
            source_id=row[0], source_kind=row[1], language=row[2] or "auto",
            full_text=row[3], segments=segs,
            duration_sec=row[5] or 0.0, transcribed_at=row[6],
            model=row[7] or "", elapsed_ms=row[8] or 0.0,
        )
