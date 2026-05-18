"""
Voice message endpoints.

Endpoints:
- POST /voice-messages — Upload voice message
- GET /voice-messages/{id} — Get metadata
- GET /voice-messages/{id}/audio — Stream audio with Range support
- GET /voice-messages/channel/{channel_id} — List channel messages
- PATCH /voice-messages/{id} — Update metadata (mark read, add transcription)
- DELETE /voice-messages/{id} — Delete message and audio file
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user_id, get_db
from app.core.logging import get_logger
from app.schemas.voice_message import (
    VoiceMessageCreate,
    VoiceMessageListResponse,
    VoiceMessageResponse,
    VoiceMessageUpdate,
)
from app.services.voice_message_service import VoiceMessageService

logger = get_logger(__name__)

router = APIRouter(prefix="/voice-messages", tags=["voice_messages"])


def _msg_to_response(msg, sender_username: str | None = None) -> VoiceMessageResponse:
    """Convert VoiceMessage model to response schema."""
    from app.services.voice_message_service import VoiceMessageService

    waveform_data = None
    if msg.waveform_data:
        try:
            import json

            waveform_data = json.loads(msg.waveform_data)
        except Exception:
            pass

    return VoiceMessageResponse(
        id=msg.id,
        channel_id=msg.channel_id,
        sender_id=msg.sender_id,
        sender_username=sender_username or msg.sender.username if msg.sender else None,
        duration_ms=msg.duration_ms,
        file_size=msg.file_size,
        mime_type=msg.mime_type,
        waveform_data=waveform_data,
        transcription=msg.transcription,
        is_read=msg.is_read,
        created_at=msg.created_at,
        updated_at=msg.updated_at,
    )


@router.post("", response_model=VoiceMessageResponse, status_code=201)
async def upload_voice_message(
    file: UploadFile = File(..., description="Audio file (mp3, wav, ogg, webm, aac, flac)"),
    duration_ms: int = Form(..., ge=1, le=3600000, description="Audio duration in milliseconds"),
    channel_id: str = Form(..., description="Target channel ID"),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> VoiceMessageResponse:
    """
    Upload voice message to channel.

    Accepts audio files up to 100 MB. Client must provide accurate duration_ms
    for proper UI visualization.

    Supported formats:
    - audio/mpeg (MP3)
    - audio/wav (WAV)
    - audio/ogg (OGG/Vorbis)
    - audio/webm (WebM/Opus)
    - audio/aac (AAC)
    - audio/flac (FLAC)

    Waveform data is automatically generated for visualization.

    **Authentication:** Requires valid JWT token
    """
    try:
        # Store file
        voice_message = await VoiceMessageService.upload_voice_message(
            db=db,
            channel_id=channel_id,
            sender_id=user_id,
            file=file,
            duration_ms=duration_ms,
        )

        logger.info(
            "voice_message_uploaded",
            voice_message_id=voice_message.id,
            user_id=user_id,
            channel_id=channel_id,
        )

        return _msg_to_response(voice_message)

    except ValueError as e:
        logger.warning("voice_message_invalid", error=str(e), user_id=user_id)
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("voice_message_upload_error", error=str(e), user_id=user_id)
        raise HTTPException(status_code=500, detail="Failed to upload voice message")


@router.get("/{voice_message_id}", response_model=VoiceMessageResponse)
async def get_voice_message(
    voice_message_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> VoiceMessageResponse:
    """Get voice message metadata."""
    try:
        voice_message = await VoiceMessageService.get_voice_message(db, voice_message_id)

        logger.info(
            "voice_message_metadata_requested",
            voice_message_id=voice_message_id,
            user_id=user_id,
        )

        return _msg_to_response(voice_message)

    except Exception as e:
        logger.warning(
            "voice_message_not_found",
            voice_message_id=voice_message_id,
            user_id=user_id,
        )
        raise HTTPException(status_code=404, detail="Voice message not found")


@router.get("/{voice_message_id}/audio")
async def stream_voice_message(
    voice_message_id: str,
    range_header: Optional[str] = Query(None, alias="Range"),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """
    Stream voice message audio with Range request support.

    Supports HTTP Range header for efficient streaming:
    - Range: bytes=0-1023 (first 1024 bytes)
    - Range: bytes=1024- (from byte 1024 to end)
    - Range: bytes=-1024 (last 1024 bytes)

    Returns:
    - 200 OK: Full file (no Range specified)
    - 206 Partial Content: Range request satisfied
    - 416 Range Not Satisfiable: Invalid range
    """
    try:
        voice_message = await VoiceMessageService.get_voice_message(db, voice_message_id)

        # Parse Range header if present
        range_start = None
        range_end = None

        if range_header:
            # Format: "bytes=start-end"
            try:
                parts = range_header.replace("bytes=", "").split("-")
                if len(parts) == 2:
                    start_str, end_str = parts
                    if start_str:
                        range_start = int(start_str)
                    if end_str:
                        range_end = int(end_str)
            except (ValueError, IndexError):
                logger.warning("invalid_range_header", range=range_header)

        # Get audio data
        audio_data, start_byte, end_byte = await VoiceMessageService.get_audio_file_data(
            voice_message, range_start, range_end
        )

        file_size = voice_message.file_size

        # Determine status code and headers
        if range_header:
            # Range request
            headers = {
                "Content-Type": voice_message.mime_type,
                "Content-Length": str(len(audio_data)),
                "Content-Range": f"bytes {start_byte}-{end_byte}/{file_size}",
                "Accept-Ranges": "bytes",
            }
            status_code = 206
        else:
            # Full file
            headers = {
                "Content-Type": voice_message.mime_type,
                "Content-Length": str(file_size),
                "Accept-Ranges": "bytes",
            }
            status_code = 200

        logger.info(
            "voice_audio_streamed",
            voice_message_id=voice_message_id,
            user_id=user_id,
            range=(f"{start_byte}-{end_byte}" if range_header else "full"),
        )

        return Response(content=audio_data, status_code=status_code, headers=headers)

    except Exception as e:
        if "not found" in str(e).lower():
            logger.warning(
                "voice_message_audio_not_found",
                voice_message_id=voice_message_id,
                user_id=user_id,
            )
            raise HTTPException(status_code=404, detail="Voice message audio not found")
        elif "invalid range" in str(e).lower():
            raise HTTPException(status_code=416, detail="Range not satisfiable")
        else:
            logger.error("voice_audio_stream_error", error=str(e), user_id=user_id)
            raise HTTPException(status_code=500, detail="Failed to stream audio")


@router.get("/channel/{channel_id}")
async def list_channel_voice_messages(
    channel_id: str,
    limit: int = Query(50, ge=1, le=200, description="Results per page"),
    offset: int = Query(0, ge=0, description="Results offset"),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> VoiceMessageListResponse:
    """
    List voice messages in channel.

    Returns messages in reverse chronological order (newest first).
    """
    try:
        messages, total = await VoiceMessageService.get_channel_voice_messages(
            db, channel_id, limit=limit, offset=offset
        )

        logger.info(
            "channel_voice_messages_listed",
            channel_id=channel_id,
            user_id=user_id,
            count=len(messages),
        )

        return VoiceMessageListResponse(
            messages=[_msg_to_response(msg) for msg in messages],
            total=total,
            has_more=(offset + limit) < total,
            limit=limit,
        )

    except Exception as e:
        logger.error("voice_message_list_error", error=str(e), user_id=user_id)
        raise HTTPException(status_code=500, detail="Failed to list voice messages")


@router.patch("/{voice_message_id}", response_model=VoiceMessageResponse)
async def update_voice_message(
    voice_message_id: str,
    body: VoiceMessageUpdate,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> VoiceMessageResponse:
    """
    Update voice message metadata.

    Allows marking as read and adding transcription text
    (typically from async speech-to-text processing).
    """
    try:
        voice_message = await VoiceMessageService.update_voice_message(
            db,
            voice_message_id,
            is_read=body.is_read,
            transcription=body.transcription,
        )

        logger.info(
            "voice_message_updated",
            voice_message_id=voice_message_id,
            user_id=user_id,
        )

        return _msg_to_response(voice_message)

    except Exception as e:
        logger.warning(
            "voice_message_update_not_found",
            voice_message_id=voice_message_id,
            user_id=user_id,
        )
        raise HTTPException(status_code=404, detail="Voice message not found")


@router.delete("/{voice_message_id}", status_code=204, response_class=Response)
async def delete_voice_message(
    voice_message_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Delete voice message and audio file."""
    try:
        await VoiceMessageService.delete_voice_message(db, voice_message_id)

        logger.info(
            "voice_message_deleted",
            voice_message_id=voice_message_id,
            user_id=user_id,
        )

        return Response(status_code=204)
    except Exception as e:
        logger.warning(
            "voice_message_delete_not_found",
            voice_message_id=voice_message_id,
            user_id=user_id,
        )
        raise HTTPException(status_code=404, detail="Voice message not found")
