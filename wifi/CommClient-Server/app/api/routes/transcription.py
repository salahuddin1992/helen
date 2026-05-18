"""
Transcription REST endpoints.

Whisper.cpp runs entirely on the host (CPU/GPU), no network calls.
Endpoints:
  POST   /api/transcripts/{source_kind}/{source_id}  trigger transcription
  GET    /api/transcripts/{source_kind}/{source_id}  fetch transcript
  GET    /api/transcripts/health                     whisper-cli readiness
  DELETE /api/transcripts/{source_kind}/{source_id}  remove transcript
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Path as PathParam
from pydantic import BaseModel

from app.core.config import get_settings
from app.core.deps import get_current_user_id
from app.core.logging import get_logger
from app.services.transcription import (
    TranscriptionError,
    TranscriptStore,
    WhisperTranscriber,
)

logger = get_logger(__name__)
router = APIRouter(prefix="/transcripts", tags=["transcription"])

_settings = get_settings()
_STORE: Optional[TranscriptStore] = None
_WHISPER: Optional[WhisperTranscriber] = None
_VALID_KINDS = {"voice_message", "call_recording"}


def _get_store() -> TranscriptStore:
    global _STORE
    if _STORE is None:
        sqlite_p = Path(_settings.SQLITE_PATH)
        base = sqlite_p.resolve().parent if sqlite_p.is_absolute() \
            else (_settings.PROJECT_ROOT / sqlite_p).resolve().parent
        base.mkdir(parents=True, exist_ok=True)
        _STORE = TranscriptStore(str(base / "transcripts.db"))
    return _STORE


def _get_whisper() -> WhisperTranscriber:
    global _WHISPER
    if _WHISPER is None:
        _WHISPER = WhisperTranscriber()
    return _WHISPER


class TranscribeRequest(BaseModel):
    audio_path: str
    language: Optional[str] = None
    max_seconds_wait: int = 600


class TranscriptResponse(BaseModel):
    source_id: str
    source_kind: str
    language: str
    full_text: str
    segments: list[dict]
    duration_sec: float
    transcribed_at: float
    model: str
    elapsed_ms: float


def _to_response(t) -> TranscriptResponse:
    return TranscriptResponse(
        source_id=t.source_id,
        source_kind=t.source_kind,
        language=t.language,
        full_text=t.full_text,
        segments=[s.__dict__ for s in t.segments],
        duration_sec=t.duration_sec,
        transcribed_at=t.transcribed_at,
        model=t.model,
        elapsed_ms=t.elapsed_ms,
    )


@router.get("/health")
async def whisper_health(
    user_id: str = Depends(get_current_user_id),
):
    """Tells the UI whether the transcribe button should be visible."""
    w = _get_whisper()
    return {
        "available": w.is_available(),
        "whisper_bin": w.whisper_bin or "",
        "model_path": w.model_path or "",
        "language": w.language,
        "threads": w.threads,
    }


@router.post("/{source_kind}/{source_id}", response_model=TranscriptResponse)
async def trigger_transcription(
    payload: TranscribeRequest,
    source_kind: str = PathParam(..., min_length=3, max_length=32),
    source_id: str = PathParam(..., min_length=1, max_length=128),
    user_id: str = Depends(get_current_user_id),
):
    """Run whisper on the audio at audio_path. Awaits the result —
    callers should treat this as a long-running request (use a long
    HTTP timeout). Result is cached per (source_kind, source_id) so
    repeat calls return instantly."""
    if source_kind not in _VALID_KINDS:
        raise HTTPException(
            status_code=400,
            detail=f"source_kind must be one of {sorted(_VALID_KINDS)}",
        )
    if not Path(payload.audio_path).is_file():
        raise HTTPException(status_code=404, detail="audio file not found")
    # If already transcribed, return cached.
    cached = _get_store().get(source_kind, source_id)
    if cached is not None and cached.full_text:
        return _to_response(cached)

    w = _get_whisper()
    if payload.language:
        w.language = payload.language
    try:
        t = await w.transcribe(
            payload.audio_path,
            source_id=source_id,
            source_kind=source_kind,
            max_seconds_wait=payload.max_seconds_wait,
        )
    except TranscriptionError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    _get_store().save(t)
    logger.info("transcript_saved",
                source_kind=source_kind, source_id=source_id,
                duration_sec=t.duration_sec, language=t.language)
    return _to_response(t)


@router.get("/{source_kind}/{source_id}", response_model=TranscriptResponse)
async def get_transcript(
    source_kind: str,
    source_id: str,
    user_id: str = Depends(get_current_user_id),
):
    if source_kind not in _VALID_KINDS:
        raise HTTPException(status_code=400, detail="invalid source_kind")
    t = _get_store().get(source_kind, source_id)
    if t is None:
        raise HTTPException(status_code=404, detail="no transcript")
    return _to_response(t)


@router.delete("/{source_kind}/{source_id}")
async def delete_transcript(
    source_kind: str,
    source_id: str,
    user_id: str = Depends(get_current_user_id),
):
    if source_kind not in _VALID_KINDS:
        raise HTTPException(status_code=400, detail="invalid source_kind")
    import sqlite3
    store = _get_store()
    with sqlite3.connect(store.db_path) as c:
        cur = c.execute(
            "DELETE FROM transcripts WHERE source_kind=? AND source_id=?",
            (source_kind, source_id),
        )
        deleted = cur.rowcount
    if deleted == 0:
        raise HTTPException(status_code=404, detail="not found")
    return {"deleted": deleted}
