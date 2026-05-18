"""
File upload/download REST endpoints.

Hardened:
  - File download requires channel membership authorization
  - File upload requires channel membership authorization
  - Content-Disposition: attachment on all downloads (prevents browser XSS)
  - MIME type validation via magic bytes
  - Audit logging for file access
"""

from __future__ import annotations

import hashlib
import os
import re
from typing import AsyncIterator

import aiofiles
from fastapi import (
    APIRouter,
    Depends,
    File,
    Header,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse, Response, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_file_access, audit_permission_denied
from app.core.config import get_settings
from app.core.deps import get_current_user_id, get_db
from app.core.upload_throttle import ThrottleError, upload_throttle
from app.schemas.file import FileResponse as FileSchema
from app.models.file import FileRecord
from app.services.channel_service import ChannelService
from app.services.file_service import FileService

router = APIRouter(prefix="/files", tags=["files"])
_settings = get_settings()

# Streaming chunk size for partial/full downloads. 1 MiB is a good balance
# between syscall overhead and memory footprint.
_DOWNLOAD_CHUNK = 1 << 20

# Only a single, ascending byte range is supported. That covers every
# mainstream HTTP client (browsers, curl --range, aria2c) and keeps the
# implementation simple. Multi-range (multipart/byteranges) is not needed.
_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


def _weak_etag(storage_path: str, size: int) -> str:
    """ETag derived from (path, size, mtime) so edits invalidate cached
    clients. Weak marker (``W/``) because the body-to-ETag relationship is
    resolution-bounded by mtime granularity."""
    try:
        st = os.stat(storage_path)
        sig = f"{storage_path}:{size}:{int(st.st_mtime_ns)}"
    except OSError:
        sig = f"{storage_path}:{size}"
    digest = hashlib.blake2b(sig.encode("utf-8"), digest_size=8).hexdigest()
    return f'W/"{digest}"'


async def _stream_range(
    path: str,
    start: int,
    end_inclusive: int,
) -> AsyncIterator[bytes]:
    """Stream [start, end_inclusive] from ``path`` in fixed chunks.

    Uses aiofiles so the event loop keeps serving other requests during
    large sequential reads.
    """
    remaining = end_inclusive - start + 1
    async with aiofiles.open(path, "rb") as f:
        await f.seek(start)
        while remaining > 0:
            chunk = await f.read(min(_DOWNLOAD_CHUNK, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


def _parse_range_header(
    range_header: str | None,
    file_size: int,
) -> tuple[int, int] | None:
    """Parse a single-range ``bytes=A-B`` header. Returns None if the
    header is absent/unsupported. Raises 416 on an unsatisfiable range."""
    if not range_header:
        return None
    m = _RANGE_RE.match(range_header.strip())
    if not m:
        return None  # malformed → ignore and serve full content
    s_str, e_str = m.group(1), m.group(2)
    if s_str == "" and e_str == "":
        return None  # bytes=-  → meaningless, fall back to full
    if s_str == "":
        # Suffix range: last N bytes
        suffix = int(e_str)
        if suffix <= 0:
            raise HTTPException(
                status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
                headers={"Content-Range": f"bytes */{file_size}"},
            )
        start = max(0, file_size - suffix)
        end = file_size - 1
    else:
        start = int(s_str)
        end = int(e_str) if e_str else file_size - 1
    if start > end or start >= file_size:
        raise HTTPException(
            status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
            headers={"Content-Range": f"bytes */{file_size}"},
        )
    end = min(end, file_size - 1)
    return start, end


async def _proxy_file_from_peer(
    file_id: str,
    user_id: str,
    range_header: str | None,
    if_range: str | None,
    channel_membership_db: AsyncSession,
):
    """Locate ``file_id`` on a sibling Helen server and proxy the bytes.

    Returns a StreamingResponse on success, or None when:
      - file isn't hosted on any reachable peer
      - the peer who has it returns a non-2xx
      - federation is mis-configured

    NOTE: this stop-gap does NOT verify authorisation against the
    REMOTE channel state — it trusts the local channel membership
    that the user is already authenticated against. In practice every
    sibling server in a Helen mesh is in the same trust domain and
    the file's channel_id is replicated, so a remote peer's record
    has the same channel_id we'd check locally. For unauthenticated
    download attempts via this path, the per-user JWT auth on the
    fronting server (already passed before reaching this function)
    is the gate.
    """
    from app.services.peer_registry import peer_registry
    from app.services.federation_service import federation_service
    import httpx as _httpx
    from fastapi.responses import StreamingResponse as _SR

    peers = await peer_registry.list(include_stale=False)
    if not peers:
        return None

    owner = None
    locate_path = f"/api/federation/files/{file_id}/locate"
    for peer in peers:
        try:
            resp = await federation_service._signed_request(
                peer, "GET", locate_path,
            )
            if resp is not None and resp.status_code == 200:
                owner = peer
                break
        except Exception:
            continue
    if owner is None:
        return None

    # Now fetch the bytes from the owner. Hand-craft the signed request
    # with Range header forwarded so resumable downloads still work
    # across the proxy hop.
    fetch_path = f"/api/federation/files/{file_id}/content"
    extra_headers = {}
    if range_header:
        extra_headers["Range"] = range_header
    if if_range:
        extra_headers["If-Range"] = if_range

    # Sign manually using federation_auth so we can stream the body.
    # Forward the acting user_id so the OWNER server can re-verify
    # channel membership against its own DB (audit fix 2.4 — closes
    # the gap where a kicked member could pull bytes during sync lag).
    from app.core.federation_auth import sign_request, HEADER_ORIGIN
    from app.services.discovery_service import get_server_id as _my_id
    sig_headers = sign_request("GET", fetch_path, b"")
    sig_headers[HEADER_ORIGIN] = _my_id()
    sig_headers["X-Federation-Acting-User"] = user_id
    sig_headers.update(extra_headers)
    url = f"{owner.protocol}://{owner.host}:{owner.port}{fetch_path}"

    client = _httpx.AsyncClient(timeout=120.0)
    try:
        upstream = await client.send(
            _httpx.Request("GET", url, headers=sig_headers),
            stream=True,
        )
    except Exception:
        await client.aclose()
        return None
    if upstream.status_code >= 400:
        try:
            await upstream.aclose()
        finally:
            await client.aclose()
        return None

    pass_headers = {
        k: v for k, v in upstream.headers.items()
        if k.lower() in {
            "content-length", "content-type", "content-range",
            "accept-ranges", "etag",
        }
    }

    async def _pump():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            try:
                await upstream.aclose()
            finally:
                await client.aclose()

    return _SR(
        _pump(),
        status_code=upstream.status_code,
        headers=pass_headers,
        media_type=pass_headers.get("content-type", "application/octet-stream"),
    )


async def _verify_file_access(
    db: AsyncSession, user_id: str, file_id: str
) -> "FileRecord":
    """
    Verify that the user has access to the file.
    A user can access a file if:
      1. They uploaded it, OR
      2. They are a member of the channel the file belongs to
    """
    record = await FileService.get_file(db, file_id)

    # Owner always has access
    if record.uploader_id == user_id:
        return record

    # If file belongs to a channel, check membership
    if record.channel_id:
        is_member = await ChannelService.is_member(db, record.channel_id, user_id)
        if not is_member:
            audit_permission_denied(user_id, f"file:{file_id}", "download")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have access to this file",
            )
        return record

    # File without channel — only uploader can access
    audit_permission_denied(user_id, f"file:{file_id}", "download")
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="You do not have access to this file",
    )


@router.post("/upload", response_model=FileSchema, status_code=201)
async def upload_file(
    file: UploadFile = File(...),
    channel_id: str | None = Query(None),
    content_length: int | None = Header(default=None, alias="Content-Length"),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    # SECURITY: Verify channel membership before allowing upload
    if channel_id:
        is_member = await ChannelService.is_member(db, channel_id, user_id)
        if not is_member:
            audit_permission_denied(user_id, f"channel:{channel_id}", "file_upload")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You must be a channel member to upload files",
            )

    # ── Upload throttle ─────────────────────────────────────────────────
    # We size against Content-Length when present. If the header is
    # missing (streamed uploads from some clients), fall back to the
    # max configured size so concurrent/count caps still fire.
    reserved_bytes = (
        content_length if (content_length and content_length > 0)
        else _settings.max_upload_bytes
    )
    try:
        await upload_throttle.acquire(user_id, reserved_bytes)
    except ThrottleError as exc:
        headers: dict[str, str] = {}
        if exc.retry_after_seconds is not None:
            headers["Retry-After"] = str(max(1, int(exc.retry_after_seconds)))
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=exc.reason,
            headers=headers or None,
        )

    success = False
    try:
        record = await FileService.upload_file(db, user_id, file, channel_id)
        audit_file_access(user_id, record.id, "upload")
        success = True
        return FileSchema(
            id=record.id,
            original_name=record.original_name,
            mime_type=record.mime_type,
            size_bytes=record.size_bytes,
            thumbnail_url=f"/api/files/{record.id}/thumbnail" if record.thumbnail_path else None,
            download_url=f"/api/files/{record.id}",
            uploader_id=record.uploader_id,
            created_at=record.created_at,
        )
    finally:
        # Refund the reservation if the upload never got past validation.
        await upload_throttle.release(user_id, success=success)


@router.get("/{file_id}")
async def download_file(
    file_id: str,
    range_header: str | None = Header(default=None, alias="Range"),
    if_range: str | None = Header(default=None, alias="If-Range"),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    # Cross-server file proxy: when this server doesn't have the file
    # locally, ask peers via federation. The user has been
    # authenticated locally and we already know which channel(s) they
    # belong to — that's the access boundary. The remote peer trusts
    # us via HMAC and serves the bytes; we proxy them back to the
    # client. No double round-trip on follow-up requests because the
    # peer remains the source of truth.
    try:
        record = await _verify_file_access(db, user_id, file_id)
    except Exception:
        # Either access denied (re-raise) OR file truly missing locally.
        # We distinguish: if the FileRecord doesn't exist, fall through
        # to federation. If it exists but access was denied, re-raise.
        from sqlalchemy import select as _sel_fr
        from app.models.file import FileRecord as _FR
        _exists_local = (await db.execute(
            _sel_fr(_FR).where(_FR.id == file_id)
        )).scalar_one_or_none()
        if _exists_local is not None:
            raise  # access denied, never federate
        # Federation fallback (only triggers when file_id is unknown locally)
        from app.core.config import get_settings as _gs
        if _gs().FEDERATION_ENABLED and _gs().FEDERATION_SECRET:
            proxied = await _proxy_file_from_peer(
                file_id=file_id,
                user_id=user_id,
                range_header=range_header,
                if_range=if_range,
                channel_membership_db=db,
            )
            if proxied is not None:
                return proxied
        from app.core.exceptions import NotFoundError as _NF
        raise _NF("File", file_id)

    if not os.path.exists(record.storage_path):
        from app.core.exceptions import NotFoundError
        raise NotFoundError("File", file_id)

    audit_file_access(user_id, file_id, "download")

    # Auto-advance the recipient's acceptance state to "delivered" on
    # first byte-level access. We don't block on it — the commit happens
    # under a fresh session so a DB hiccup can never break the download.
    if record.channel_id and record.uploader_id != user_id:
        try:
            from app.services.file_acceptance_service import (
                FileAcceptanceService,
            )
            await FileAcceptanceService.ensure_rows_for_channel_file(
                db,
                file_id=file_id,
                channel_id=record.channel_id,
                uploader_id=record.uploader_id,
            )
            row, advanced = await FileAcceptanceService.mark_delivered(
                db, file_id=file_id, recipient_id=user_id,
                bytes_received=record.size_bytes,
            )
            await db.commit()
            if advanced:
                # Lazy-import to avoid circular import on cold start.
                from app.socket.server import sio as _sio
                if _sio:
                    try:
                        await _sio.emit(
                            "file_acceptance:updated",
                            {
                                "file_id": row.file_id,
                                "channel_id": row.channel_id,
                                "recipient_id": row.recipient_id,
                                "state": row.state,
                                "advanced": True,
                                "bytes_received": row.bytes_received,
                                "delivered_at": row.delivered_at.isoformat() if row.delivered_at else None,
                                "acted_at": row.acted_at.isoformat() if row.acted_at else None,
                            },
                            room=f"channel:{row.channel_id}",
                        )
                    except Exception:
                        pass
        except Exception:
            # Best-effort — never fail the download on tracking error.
            pass

    # ── Range-aware streaming (resumable downloads) ────────────────────
    # Clients that disconnect mid-download re-request with
    # ``Range: bytes=<last_received>-`` and we replay the tail. Full
    # downloads still work — no Range header → whole file is streamed.
    try:
        size = os.path.getsize(record.storage_path)
    except OSError:
        from app.core.exceptions import NotFoundError
        raise NotFoundError("File", file_id)

    etag = _weak_etag(record.storage_path, size)
    media_type = record.mime_type or "application/octet-stream"
    disposition = f'attachment; filename="{record.original_name}"'

    # If-Range: if the validator mismatches, serve the full entity (200).
    # We use the ETag as our only validator here; Last-Modified would
    # need timezone-safe handling and adds nothing on top.
    range_req = range_header
    if range_req and if_range and if_range.strip() != etag:
        range_req = None

    parsed = _parse_range_header(range_req, size)

    common_headers = {
        "Content-Disposition": disposition,
        "X-Content-Type-Options": "nosniff",
        "Accept-Ranges": "bytes",
        "ETag": etag,
    }

    if parsed is None:
        # Full content — keep FileResponse to leverage its sendfile path.
        return FileResponse(
            record.storage_path,
            media_type=media_type,
            filename=record.original_name,
            headers=common_headers,
        )

    start, end = parsed
    length = end - start + 1
    headers = {
        **common_headers,
        "Content-Range": f"bytes {start}-{end}/{size}",
        "Content-Length": str(length),
    }
    return StreamingResponse(
        _stream_range(record.storage_path, start, end),
        status_code=status.HTTP_206_PARTIAL_CONTENT,
        media_type=media_type,
        headers=headers,
    )


@router.get("/{file_id}/thumbnail")
async def get_thumbnail(
    file_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    # SECURITY: Verify access authorization
    record = await _verify_file_access(db, user_id, file_id)

    if not record.thumbnail_path or not os.path.exists(record.thumbnail_path):
        from app.core.exceptions import NotFoundError
        raise NotFoundError("Thumbnail", file_id)

    return FileResponse(
        record.thumbnail_path,
        media_type="image/jpeg",
        headers={"X-Content-Type-Options": "nosniff"},
    )


@router.delete("/{file_id}", status_code=204, response_class=Response)
async def delete_file(
    file_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> Response:
    audit_file_access(user_id, file_id, "delete")
    await FileService.delete_file(db, file_id, user_id)
    return Response(status_code=204)
