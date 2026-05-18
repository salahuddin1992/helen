"""
File drop REST endpoints — chunked uploads, progress tracking, shared folders.

Hardened:
  - Transfer init requires sender verification
  - Chunk upload validates transfer state and authorization
  - Completion verifies checksum before indexing
  - Shared folder requires channel membership
"""

from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_permission_denied
from app.core.deps import get_current_user_id, get_db
from app.core.exceptions import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.schemas.file_drop import (
    FileTransferChunk,
    FileTransferInit,
    FileTransferListResponse,
    FileTransferResponse,
    FileTransferStatus,
    SharedFolderAddFile,
    SharedFolderCreate,
    SharedFolderFile,
    SharedFolderResponse,
)
from app.services.channel_service import ChannelService
from app.services.file_drop_service import FileDropService

logger = get_logger(__name__)
router = APIRouter(prefix="/file-drop", tags=["file-drop"])


async def _verify_channel_access(
    db: AsyncSession, user_id: str, channel_id: str
) -> None:
    """Verify user is channel member."""
    is_member = await ChannelService.is_member(db, channel_id, user_id)
    if not is_member:
        audit_permission_denied(user_id, f"channel:{channel_id}", "file_drop_access")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You must be a channel member",
        )


@router.post("/init", response_model=FileTransferResponse, status_code=201)
async def init_file_transfer(
    req: FileTransferInit,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """Initialize chunked file transfer."""
    # Validate: either receiver_id (DM) or channel_id (group)
    if req.receiver_id:
        # DM transfer — verify receiver exists
        from app.services.user_service import UserService
        try:
            await UserService.get_user(db, req.receiver_id)
        except NotFoundError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Receiver not found",
            )
    elif req.channel_id:
        # Group transfer — verify membership
        await _verify_channel_access(db, user_id, req.channel_id)
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Either receiver_id or channel_id required",
        )

    transfer = await FileDropService.init_transfer(
        db,
        sender_id=user_id,
        filename=req.filename,
        file_size=req.file_size,
        mime_type=req.mime_type,
        checksum=req.checksum,
        receiver_id=req.receiver_id,
        channel_id=req.channel_id,
    )

    return FileTransferResponse(
        **{c.name: getattr(transfer, c.name) for c in transfer.__table__.columns}
    )


@router.post("/{transfer_id}/chunk", status_code=204, response_class=Response)
async def upload_chunk(
    transfer_id: str,
    chunk: FileTransferChunk,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Upload a single chunk."""
    transfer = await FileDropService.get_transfer_status(db, transfer_id)

    # Verify authorization
    if transfer.sender_id != user_id:
        audit_permission_denied(user_id, f"transfer:{transfer_id}", "upload_chunk")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only sender can upload chunks",
        )

    # Validate chunk index
    if chunk.chunk_index >= transfer.total_chunks:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid chunk index {chunk.chunk_index}",
        )

    await FileDropService.receive_chunk(db, transfer_id, chunk.chunk_index, chunk.chunk_data)
    logger.info(
        "chunk_uploaded",
        transfer_id=transfer_id,
        chunk_index=chunk.chunk_index,
        user_id=user_id,
    )
    return Response(status_code=204)


@router.post("/{transfer_id}/complete", response_model=FileTransferResponse)
async def complete_file_transfer(
    transfer_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """Complete transfer, verify checksum, assemble file."""
    transfer = await FileDropService.get_transfer_status(db, transfer_id)

    # Verify authorization
    if transfer.sender_id != user_id:
        audit_permission_denied(user_id, f"transfer:{transfer_id}", "complete_transfer")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only sender can complete transfer",
        )

    try:
        transfer = await FileDropService.complete_transfer(db, transfer_id)
    except ValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )

    return FileTransferResponse(
        **{c.name: getattr(transfer, c.name) for c in transfer.__table__.columns}
    )


@router.post("/{transfer_id}/cancel", status_code=204, response_class=Response)
async def cancel_file_transfer(
    transfer_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Cancel transfer and cleanup temp files."""
    transfer = await FileDropService.get_transfer_status(db, transfer_id)

    if transfer.sender_id != user_id:
        audit_permission_denied(user_id, f"transfer:{transfer_id}", "cancel_transfer")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only sender can cancel transfer",
        )

    await FileDropService.cancel_transfer(db, transfer_id, user_id)
    logger.info("transfer_cancelled", transfer_id=transfer_id, user_id=user_id)
    return Response(status_code=204)


@router.get("/{transfer_id}/status", response_model=FileTransferStatus)
async def get_transfer_status(
    transfer_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """Get current transfer progress."""
    transfer = await FileDropService.get_transfer_status(db, transfer_id)

    # Verify authorization
    if transfer.sender_id != user_id and transfer.receiver_id != user_id:
        if transfer.channel_id:
            await _verify_channel_access(db, user_id, transfer.channel_id)
        else:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not authorized to view this transfer",
            )

    progress = (
        (transfer.received_chunks / transfer.total_chunks * 100)
        if transfer.total_chunks > 0
        else 0
    )

    return FileTransferStatus(
        id=transfer.id,
        filename=transfer.filename,
        file_size=transfer.file_size,
        status=transfer.status,
        total_chunks=transfer.total_chunks,
        received_chunks=transfer.received_chunks,
        progress_percent=progress,
        speed_bps=transfer.speed_bps,
        error_message=transfer.error_message,
        created_at=transfer.created_at,
        updated_at=transfer.updated_at,
    )


@router.get("/active", response_model=FileTransferListResponse)
async def list_active_transfers(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """List user's active transfers."""
    transfers, total = await FileDropService.list_active_transfers(
        db, user_id, page=page, per_page=per_page
    )

    return FileTransferListResponse(
        transfers=[
            FileTransferResponse(
                **{c.name: getattr(t, c.name) for c in t.__table__.columns}
            )
            for t in transfers
        ],
        total=total,
    )


@router.post("/shared-folders", response_model=SharedFolderResponse, status_code=201)
async def create_shared_folder(
    channel_id: str = Query(...),
    req: SharedFolderCreate = ...,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """Create shared folder for channel."""
    await _verify_channel_access(db, user_id, channel_id)

    folder = await FileDropService.create_shared_folder(
        db,
        channel_id=channel_id,
        user_id=user_id,
        name=req.name,
        max_size_bytes=req.max_size_bytes,
    )

    return SharedFolderResponse(
        **{c.name: getattr(folder, c.name) for c in folder.__table__.columns},
        files=[],
    )


@router.get("/shared-folders/channel/{channel_id}", response_model=SharedFolderResponse)
async def get_channel_shared_folder(
    channel_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """Get channel's shared folder."""
    await _verify_channel_access(db, user_id, channel_id)

    folder = await FileDropService.get_shared_folder(db, channel_id)
    files = await FileDropService.list_shared_folder(db, folder.id)

    return SharedFolderResponse(
        **{c.name: getattr(folder, c.name) for c in folder.__table__.columns},
        files=[
            SharedFolderFile(
                id=sf.id,
                filename=sf.file_record.original_name,
                mime_type=sf.file_record.mime_type,
                file_size=sf.file_record.size_bytes,
                path_in_folder=sf.path_in_folder,
                added_by=sf.added_by,
                created_at=sf.created_at,
            )
            for sf in files
        ],
    )


@router.get("/shared-folders/{folder_id}/files", response_model=SharedFolderResponse)
async def list_shared_folder_files(
    folder_id: str,
    path_prefix: str | None = Query(None),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """List files in shared folder."""
    from app.models.file_drop import SharedFolder

    result = await db.execute(select(SharedFolder).where(SharedFolder.id == folder_id))
    folder = result.scalar_one_or_none()
    if not folder:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Shared folder not found",
        )

    # Verify channel access
    await _verify_channel_access(db, user_id, folder.channel_id)

    files = await FileDropService.list_shared_folder(db, folder_id, path_prefix)

    return SharedFolderResponse(
        **{c.name: getattr(folder, c.name) for c in folder.__table__.columns},
        files=[
            SharedFolderFile(
                id=sf.id,
                filename=sf.file_record.original_name,
                mime_type=sf.file_record.mime_type,
                file_size=sf.file_record.size_bytes,
                path_in_folder=sf.path_in_folder,
                added_by=sf.added_by,
                created_at=sf.created_at,
            )
            for sf in files
        ],
    )


@router.post("/shared-folders/{folder_id}/files", response_model=SharedFolderFile, status_code=201)
async def add_file_to_shared_folder(
    folder_id: str,
    req: SharedFolderAddFile,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """Add file to shared folder."""
    from app.models.file_drop import SharedFolder

    result = await db.execute(select(SharedFolder).where(SharedFolder.id == folder_id))
    folder = result.scalar_one_or_none()
    if not folder:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Shared folder not found",
        )

    # Verify channel access
    await _verify_channel_access(db, user_id, folder.channel_id)

    try:
        sf_file = await FileDropService.add_to_shared_folder(
            db, folder_id, req.file_id, user_id, req.path_in_folder
        )
    except ValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )

    return SharedFolderFile(
        id=sf_file.id,
        filename=sf_file.file_record.original_name,
        mime_type=sf_file.file_record.mime_type,
        file_size=sf_file.file_record.size_bytes,
        path_in_folder=sf_file.path_in_folder,
        added_by=sf_file.added_by,
        created_at=sf_file.created_at,
    )


@router.delete("/shared-folders/{folder_id}/files/{file_id}", status_code=204, response_class=Response)
async def remove_file_from_shared_folder(
    folder_id: str,
    file_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Remove file from shared folder."""
    from app.models.file_drop import SharedFolder

    result = await db.execute(select(SharedFolder).where(SharedFolder.id == folder_id))
    folder = result.scalar_one_or_none()
    if not folder:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Shared folder not found",
        )

    # Verify channel access
    await _verify_channel_access(db, user_id, folder.channel_id)

    await FileDropService.remove_from_shared_folder(db, folder_id, file_id, user_id)
    logger.info(
        "file_removed_from_shared_folder",
        folder_id=folder_id,
        file_id=file_id,
        user_id=user_id,
    )
    return Response(status_code=204)


# Import for select
from sqlalchemy import select
