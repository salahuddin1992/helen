"""
Resumable upload REST endpoints.

Prefixed under ``/api/files/resumable``. Complements (does NOT replace) the
legacy ``/api/files`` single-POST upload. Use this route for any file the
client wants to be able to retry after a disconnect.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Header,
    Path,
    Request,
    Response,
    status,
)
from pydantic import BaseModel, Field

from app.core.deps import get_current_user_id
from app.core.logging import get_logger
from app.core.upload_throttle import ThrottleError, upload_throttle
from app.services.channel_service import ChannelService
from app.services.resumable_upload_service import (
    ChunkIntegrityError,
    ResumableUploadError,
    SessionNotFoundError,
    SessionStateError,
    resumable_upload_service,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/files/resumable", tags=["files", "resumable"])


# ─── Schemas ────────────────────────────────────────────────────────────────

class InitRequest(BaseModel):
    filename: str = Field(..., min_length=1, max_length=512)
    total_size: int = Field(..., gt=0)
    mime_type: str | None = Field(default=None, max_length=128)
    chunk_size: int | None = Field(default=None, ge=16 * 1024, le=4 * 1024 * 1024)
    expected_sha256: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{64}$")
    channel_id: str | None = Field(default=None, max_length=32)
    metadata: dict[str, Any] | None = None


class CompleteRequest(BaseModel):
    expected_sha256: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{64}$")


# ─── Helpers ────────────────────────────────────────────────────────────────

_CONTENT_RANGE_RE = re.compile(r"^bytes\s+(\d+)-(\d+)/(\d+|\*)$", re.IGNORECASE)


def _parse_content_range(header: str | None) -> tuple[int, int, int | None] | None:
    if not header:
        return None
    m = _CONTENT_RANGE_RE.match(header.strip())
    if not m:
        return None
    start = int(m.group(1))
    end = int(m.group(2))
    total_str = m.group(3)
    total = None if total_str == "*" else int(total_str)
    return start, end, total


def _handle_service_error(exc: Exception) -> HTTPException:
    if isinstance(exc, SessionNotFoundError):
        return HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, ChunkIntegrityError):
        return HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    if isinstance(exc, SessionStateError):
        return HTTPException(status.HTTP_409_CONFLICT, detail=str(exc))
    if isinstance(exc, ResumableUploadError):
        return HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail="upload error")


# ─── Endpoints ──────────────────────────────────────────────────────────────

@router.post("/init", status_code=status.HTTP_201_CREATED)
async def init_upload(
    body: InitRequest,
    user_id: str = Depends(get_current_user_id),
) -> dict[str, Any]:
    """Open a new upload session."""
    # Verify channel membership if a channel was specified.
    if body.channel_id:
        from app.db.session import async_session_factory
        async with async_session_factory() as db:
            is_member = await ChannelService.is_member(db, body.channel_id, user_id)
        if not is_member:
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="not a channel member")

    # ── Upload throttle (reserve the declared total_size upfront) ───────
    try:
        await upload_throttle.acquire(user_id, body.total_size)
    except ThrottleError as exc:
        headers: dict[str, str] = {}
        if exc.retry_after_seconds is not None:
            headers["Retry-After"] = str(max(1, int(exc.retry_after_seconds)))
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=exc.reason,
            headers=headers or None,
        )

    try:
        try:
            result = await resumable_upload_service.init_session(
                owner_id=user_id,
                filename=body.filename,
                total_size=body.total_size,
                mime_type=body.mime_type,
                chunk_size=body.chunk_size or 1 << 18,
                expected_sha256=body.expected_sha256,
                channel_id=body.channel_id,
                metadata=body.metadata,
            )
        except Exception as exc:
            logger.warning("upload_init_failed", owner=user_id, error=str(exc))
            # Init failed — roll back the reservation so the byte quota
            # isn't charged for a session that never existed.
            await upload_throttle.release(user_id, success=False)
            raise _handle_service_error(exc)
    except HTTPException:
        raise

    # Init succeeded → the throttle "counts" this upload toward the
    # rolling byte quota (window entry stays), but release the
    # concurrency slot right away. Sessions govern their own concurrency
    # via resumable_upload_service, so holding the slot past init would
    # artificially cap a user at UPLOAD_MAX_CONCURRENT simultaneous
    # *sessions* regardless of whether they're actively transferring.
    await upload_throttle.release_inflight(user_id)
    return result


@router.put("/{session_id}/chunk/{index}")
async def put_chunk(
    request: Request,
    session_id: str = Path(..., max_length=32),
    index: int = Path(..., ge=0),
    content_range: str | None = Header(default=None, alias="Content-Range"),
    x_chunk_crc32: str | None = Header(default=None, alias="X-Chunk-CRC32"),
    x_chunk_sha256: str | None = Header(default=None, alias="X-Chunk-SHA256"),
    user_id: str = Depends(get_current_user_id),
) -> dict[str, Any]:
    # Parse Content-Range for offset sanity check (optional)
    parsed = _parse_content_range(content_range)
    offset_hint = parsed[0] if parsed else None

    try:
        raw = await request.body()
    except Exception as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"body read failed: {exc}")

    if not raw:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="empty chunk")
    if len(raw) > 4 * 1024 * 1024:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="chunk too big")

    try:
        crc = int(x_chunk_crc32) if x_chunk_crc32 is not None else None
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="invalid X-Chunk-CRC32")

    try:
        result = await resumable_upload_service.put_chunk(
            session_id=session_id,
            owner_id=user_id,
            index=index,
            data=raw,
            expected_crc32=crc,
            expected_sha256=x_chunk_sha256,
            offset_hint=offset_hint,
        )
    except Exception as exc:
        logger.warning(
            "upload_chunk_failed",
            owner=user_id, session=session_id, idx=index, error=str(exc),
        )
        raise _handle_service_error(exc)
    return result


@router.get("/{session_id}/status")
async def upload_status(
    session_id: str = Path(..., max_length=32),
    user_id: str = Depends(get_current_user_id),
) -> dict[str, Any]:
    try:
        return await resumable_upload_service.get_status(session_id, user_id)
    except Exception as exc:
        raise _handle_service_error(exc)


@router.post("/{session_id}/complete")
async def complete_upload(
    body: CompleteRequest,
    session_id: str = Path(..., max_length=32),
    user_id: str = Depends(get_current_user_id),
) -> dict[str, Any]:
    try:
        return await resumable_upload_service.complete(
            session_id, user_id, expected_sha256=body.expected_sha256,
        )
    except Exception as exc:
        raise _handle_service_error(exc)


@router.delete(
    "/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def abort_upload(
    session_id: str = Path(..., max_length=32),
    user_id: str = Depends(get_current_user_id),
):
    try:
        await resumable_upload_service.abort(session_id, user_id)
    except Exception as exc:
        raise _handle_service_error(exc)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
