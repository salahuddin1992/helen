"""
Voice message storage, processing, and streaming service.

Features:
- Audio upload with validation
- Real waveform data generation via ffmpeg PCM decode (with byte-variance fallback)
- Range request support for efficient streaming
- Automatic cleanup on deletion
- Transcription storage (empty until async processing)
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import shutil
import struct
import tempfile
from pathlib import Path

import aiofiles
from fastapi import UploadFile
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.models.voice_message import VoiceMessage

logger = get_logger(__name__)
settings = get_settings()

# Supported audio MIME types
ALLOWED_AUDIO_TYPES = {
    "audio/mpeg",
    "audio/mp3",
    "audio/wav",
    "audio/ogg",
    "audio/webm",
    "audio/aac",
    "audio/flac",
}

# Max audio file size (100 MB)
MAX_AUDIO_SIZE = 100 * 1024 * 1024

# Max duration (1 hour)
MAX_DURATION_MS = 3600000


class VoiceMessageService:
    """Voice message service — upload, storage, streaming, waveform generation."""

    # Voice messages stored in separate subdirectory
    VOICE_SUBDIR = "voice_messages"

    @staticmethod
    def _get_voice_dir() -> Path:
        """Get or create voice messages storage directory."""
        voice_dir = settings.upload_path / VoiceMessageService.VOICE_SUBDIR
        voice_dir.mkdir(parents=True, exist_ok=True)
        return voice_dir

    # ─────────────────────────────────────────────────────────────────────────
    # Upload & Storage
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    async def upload_voice_message(
        db: AsyncSession,
        channel_id: str,
        sender_id: str,
        file: UploadFile,
        duration_ms: int,
    ) -> VoiceMessage:
        """
        Upload and store voice message.

        Args:
            db: Database session
            channel_id: Channel ID
            sender_id: Sender user ID
            file: Uploaded audio file
            duration_ms: Audio duration in milliseconds

        Returns:
            VoiceMessage record

        Raises:
            ValidationError: Invalid file or parameters
        """
        # Validate MIME type
        if file.content_type not in ALLOWED_AUDIO_TYPES:
            raise ValidationError(
                f"Audio type '{file.content_type}' not supported. "
                f"Allowed: {', '.join(sorted(ALLOWED_AUDIO_TYPES))}"
            )

        # Validate duration
        if not (0 < duration_ms <= MAX_DURATION_MS):
            raise ValidationError(f"Duration must be between 1ms and {MAX_DURATION_MS}ms")

        # Read content
        content = await file.read()
        file_size = len(content)

        if file_size > MAX_AUDIO_SIZE:
            raise ValidationError(
                f"File too large ({file_size} bytes). Max: {MAX_AUDIO_SIZE} bytes"
            )

        if file_size == 0:
            raise ValidationError("Empty audio file")

        # Generate storage path
        voice_dir = VoiceMessageService._get_voice_dir()
        file_id = (
            f"{channel_id}_{sender_id}_{int(file.file_object.tell())}"
            if hasattr(file, "file_object")
            else f"{channel_id}_{sender_id}"
        )

        # Simpler naming: use UUID from message ID (will be set after creation)
        import uuid

        voice_id = uuid.uuid4().hex
        ext = Path(file.filename or "audio.mp3").suffix.lower()
        if ext not in {".mp3", ".wav", ".ogg", ".webm", ".aac", ".flac"}:
            ext = ".mp3"

        stored_name = f"{voice_id}{ext}"
        storage_path = voice_dir / stored_name

        # Write to disk
        async with aiofiles.open(storage_path, "wb") as f:
            await f.write(content)

        # Generate waveform data
        waveform_samples = await VoiceMessageService._generate_waveform(
            content, duration_ms, samples=100
        )

        # Create database record
        record = VoiceMessage(
            channel_id=channel_id,
            sender_id=sender_id,
            duration_ms=duration_ms,
            file_path=str(storage_path),
            file_size=file_size,
            mime_type=file.content_type or "audio/mpeg",
            waveform_data=json.dumps(waveform_samples),
        )

        db.add(record)
        await db.commit()
        await db.refresh(record)

        logger.info(
            "voice_message_uploaded",
            voice_message_id=record.id,
            channel_id=channel_id,
            sender_id=sender_id,
            file_size=file_size,
            duration_ms=duration_ms,
        )

        return record

    # ─────────────────────────────────────────────────────────────────────────
    # Waveform Generation
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    async def _generate_waveform(
        audio_content: bytes, duration_ms: int, samples: int = 100
    ) -> list[float]:
        """
        Generate a waveform peak array (length == `samples`) from raw audio bytes.

        Strategy:
          1. If ffmpeg is available, decode to mono 16-bit PCM at 8 kHz, then
             compute the peak |sample|/32768 over `samples` equally-sized buckets.
          2. Otherwise fall back to a byte-variance approximation so the API
             continues to function on hosts without ffmpeg installed.

        Returns: list[float] of length `samples`, each in [0.0, 1.0].
        """
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg:
            try:
                pcm = await VoiceMessageService._decode_pcm_with_ffmpeg(
                    ffmpeg, audio_content
                )
                if pcm:
                    return VoiceMessageService._pcm_to_peaks(pcm, samples)
            except Exception as e:
                logger.warning("waveform_ffmpeg_decode_failed", error=str(e))

        # Fallback — byte variance heuristic (keeps API working without ffmpeg)
        return VoiceMessageService._byte_variance_waveform(audio_content, samples)

    @staticmethod
    async def _decode_pcm_with_ffmpeg(ffmpeg: str, audio_content: bytes) -> bytes:
        """
        Run ffmpeg to decode arbitrary audio bytes to mono 16-bit PCM @ 8 kHz.
        Returns raw PCM bytes (little-endian signed 16-bit).
        """
        # Write input to a temp file (more reliable across formats than stdin pipe)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".audio") as f:
            f.write(audio_content)
            tmp_path = f.name
        try:
            proc = await asyncio.create_subprocess_exec(
                ffmpeg,
                "-hide_banner", "-loglevel", "error",
                "-i", tmp_path,
                "-ac", "1",          # mono
                "-ar", "8000",       # 8 kHz — enough for waveform
                "-f", "s16le",
                "-acodec", "pcm_s16le",
                "pipe:1",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=20.0)
            except asyncio.TimeoutError:
                proc.kill()
                raise RuntimeError("ffmpeg timed out generating waveform")
            if proc.returncode != 0:
                raise RuntimeError(
                    f"ffmpeg exit {proc.returncode}: {stderr.decode(errors='ignore')[:200]}"
                )
            return stdout
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    @staticmethod
    def _pcm_to_peaks(pcm_bytes: bytes, samples: int) -> list[float]:
        """
        Convert little-endian signed 16-bit PCM to a peak array.
        """
        if not pcm_bytes:
            return [0.0] * samples
        n_samples = len(pcm_bytes) // 2
        if n_samples == 0:
            return [0.0] * samples

        # Use struct to unpack — fast enough for short voice messages
        # For long ones, this is still O(n) once.
        fmt = f"<{n_samples}h"
        try:
            pcm = struct.unpack(fmt, pcm_bytes[: n_samples * 2])
        except struct.error:
            return [0.0] * samples

        bucket_size = max(1, n_samples // samples)
        peaks: list[float] = []
        for i in range(samples):
            start = i * bucket_size
            end = start + bucket_size
            if start >= n_samples:
                peaks.append(0.0)
                continue
            chunk = pcm[start:end]
            if not chunk:
                peaks.append(0.0)
                continue
            # Use RMS for a more pleasing visual than raw peak
            sq_sum = 0
            for s in chunk:
                sq_sum += s * s
            rms = math.sqrt(sq_sum / len(chunk)) / 32768.0
            peaks.append(min(1.0, round(rms, 4)))
        # Pad if we somehow under-filled (e.g. very short PCM)
        while len(peaks) < samples:
            peaks.append(0.0)
        return peaks[:samples]

    @staticmethod
    def _byte_variance_waveform(audio_content: bytes, samples: int) -> list[float]:
        """Fallback waveform — used when ffmpeg is unavailable."""
        if not audio_content:
            return [0.0] * samples
        if len(audio_content) < samples:
            samples = max(1, len(audio_content))

        chunk_size = max(1, len(audio_content) // samples)
        waveform: list[float] = []
        for i in range(samples):
            start = i * chunk_size
            end = min(start + chunk_size, len(audio_content))
            chunk = audio_content[start:end]
            if chunk:
                peak = (max(chunk) - min(chunk)) / 255.0
                waveform.append(min(1.0, round(peak, 4)))
            else:
                waveform.append(0.0)
        return waveform

    @staticmethod
    async def get_waveform_data(voice_message: VoiceMessage) -> list[float]:
        """Get waveform data as list of floats."""
        if not voice_message.waveform_data:
            return []
        try:
            return json.loads(voice_message.waveform_data)
        except json.JSONDecodeError:
            logger.warning(
                "invalid_waveform_data",
                voice_message_id=voice_message.id,
            )
            return []

    # ─────────────────────────────────────────────────────────────────────────
    # Retrieval & Metadata
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    async def get_voice_message(db: AsyncSession, voice_message_id: str) -> VoiceMessage:
        """Get voice message by ID."""
        result = await db.execute(
            select(VoiceMessage).where(VoiceMessage.id == voice_message_id)
        )
        record = result.scalar_one_or_none()
        if not record:
            raise NotFoundError("VoiceMessage", voice_message_id)
        return record

    @staticmethod
    async def get_channel_voice_messages(
        db: AsyncSession,
        channel_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[VoiceMessage], int]:
        """
        Get voice messages for channel with pagination.

        Returns:
            Tuple of (messages, total_count)
        """
        # Get total count
        count_result = await db.execute(
            select(func.count(VoiceMessage.id)).where(VoiceMessage.channel_id == channel_id)
        )
        total = count_result.scalar() or 0

        # Get messages (newest first)
        result = await db.execute(
            select(VoiceMessage)
            .where(VoiceMessage.channel_id == channel_id)
            .order_by(VoiceMessage.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        messages = result.scalars().all()

        return list(messages), total

    # ─────────────────────────────────────────────────────────────────────────
    # File Access
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    async def get_audio_file_data(
        voice_message: VoiceMessage, range_start: int | None = None, range_end: int | None = None
    ) -> tuple[bytes, int, int]:
        """
        Read audio file with optional range request support.

        Args:
            voice_message: VoiceMessage record
            range_start: Start byte (inclusive)
            range_end: End byte (inclusive)

        Returns:
            Tuple of (data, start_byte, end_byte)

        Raises:
            NotFoundError: File not found on disk
            ValidationError: Invalid range
        """
        if not Path(voice_message.file_path).exists():
            logger.error(
                "voice_file_not_found",
                voice_message_id=voice_message.id,
                file_path=voice_message.file_path,
            )
            raise NotFoundError("Audio file", voice_message.file_path)

        # Read full file
        async with aiofiles.open(voice_message.file_path, "rb") as f:
            full_data = await f.read()

        file_size = len(full_data)

        # Handle range requests
        if range_start is None:
            range_start = 0
        if range_end is None:
            range_end = file_size - 1

        # Validate range
        if range_start < 0 or range_end >= file_size or range_start > range_end:
            raise ValidationError(f"Invalid range: {range_start}-{range_end}/{file_size}")

        data = full_data[range_start : range_end + 1]
        return data, range_start, range_end

    # ─────────────────────────────────────────────────────────────────────────
    # Updates & Deletion
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    async def update_voice_message(
        db: AsyncSession,
        voice_message_id: str,
        is_read: bool | None = None,
        transcription: str | None = None,
    ) -> VoiceMessage:
        """Update voice message metadata."""
        record = await VoiceMessageService.get_voice_message(db, voice_message_id)

        if is_read is not None:
            record.is_read = is_read

        if transcription is not None:
            record.transcription = transcription

        await db.commit()
        await db.refresh(record)

        logger.info(
            "voice_message_updated",
            voice_message_id=voice_message_id,
            is_read=is_read,
            has_transcription=transcription is not None,
        )

        return record

    @staticmethod
    async def delete_voice_message(db: AsyncSession, voice_message_id: str) -> bool:
        """Delete voice message and cleanup audio file."""
        record = await VoiceMessageService.get_voice_message(db, voice_message_id)

        # Delete audio file
        try:
            file_path = Path(record.file_path)
            if file_path.exists():
                file_path.unlink()
                logger.info(
                    "voice_file_deleted",
                    voice_message_id=voice_message_id,
                    file_path=record.file_path,
                )
        except Exception as e:
            logger.warning(
                "voice_file_delete_error",
                voice_message_id=voice_message_id,
                file_path=record.file_path,
                error=str(e),
            )

        # Delete database record
        await db.delete(record)
        await db.commit()

        logger.info(
            "voice_message_deleted",
            voice_message_id=voice_message_id,
            channel_id=record.channel_id,
        )

        return True
