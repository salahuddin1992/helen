"""
File drop service — chunked uploads, transfer progress, shared folders.
Supports LAN-optimized file sharing with progress tracking and deduplication.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiofiles
from sqlalchemy import and_, desc, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import get_settings
from app.core.exceptions import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.models.file_drop import FileTransfer, SharedFolder, SharedFolderFile
from app.models.file import FileRecord

logger = get_logger(__name__)
settings = get_settings()


class FileDropService:
    """Service for chunked file transfers, progress tracking, and shared folders."""

    # Track active transfers in memory for quick lookups
    _active_transfers: dict[str, dict] = {}

    @staticmethod
    async def init_transfer(
        db: AsyncSession,
        sender_id: str,
        filename: str,
        file_size: int,
        mime_type: str,
        checksum: str,
        receiver_id: str | None = None,
        channel_id: str | None = None,
    ) -> FileTransfer:
        """
        Initialize a chunked file transfer.
        Creates temp directory and transfer record.
        """
        if not receiver_id and not channel_id:
            raise ValidationError("Either receiver_id or channel_id must be provided")

        # Calculate chunk info (16MB chunks)
        chunk_size = 16 * 1024 * 1024
        total_chunks = (file_size + chunk_size - 1) // chunk_size

        # Create temp directory for this transfer
        temp_dir = Path(tempfile.gettempdir()) / f"file_transfer_{hashlib.md5(checksum.encode()).hexdigest()}"
        temp_dir.mkdir(parents=True, exist_ok=True)

        transfer = FileTransfer(
            sender_id=sender_id,
            receiver_id=receiver_id,
            channel_id=channel_id,
            filename=filename,
            file_size=file_size,
            mime_type=mime_type,
            chunk_size=chunk_size,
            total_chunks=total_chunks,
            checksum=checksum,
            status="pending",
        )
        db.add(transfer)
        await db.commit()
        await db.refresh(transfer)

        # Track in memory
        FileDropService._active_transfers[transfer.id] = {
            "temp_dir": str(temp_dir),
            "start_time": time.time(),
            "bytes_received": 0,
        }

        logger.info(
            "file_transfer_initiated",
            transfer_id=transfer.id,
            filename=filename,
            file_size=file_size,
            total_chunks=total_chunks,
        )
        return transfer

    @staticmethod
    async def receive_chunk(
        db: AsyncSession,
        transfer_id: str,
        chunk_index: int,
        chunk_data: bytes,
    ) -> None:
        """Receive and write a single chunk."""
        transfer = await db.execute(
            select(FileTransfer).where(FileTransfer.id == transfer_id)
        )
        transfer = transfer.scalar_one_or_none()
        if not transfer:
            raise NotFoundError("FileTransfer", transfer_id)

        if transfer.status != "pending" and transfer.status != "uploading":
            raise ValidationError(f"Transfer is {transfer.status}, cannot receive chunks")

        # Get temp directory
        if transfer_id not in FileDropService._active_transfers:
            raise ValidationError("Transfer context not found")

        meta = FileDropService._active_transfers[transfer_id]
        temp_dir = Path(meta["temp_dir"])
        chunk_path = temp_dir / f"chunk_{chunk_index:06d}"

        # Write chunk
        async with aiofiles.open(chunk_path, "wb") as f:
            await f.write(chunk_data)

        # Update transfer
        transfer.status = "uploading"
        transfer.received_chunks = chunk_index + 1
        meta["bytes_received"] += len(chunk_data)
        elapsed = time.time() - meta["start_time"]
        transfer.speed_bps = meta["bytes_received"] / elapsed if elapsed > 0 else 0

        await db.commit()

        logger.debug(
            "file_transfer_chunk_received",
            transfer_id=transfer_id,
            chunk_index=chunk_index,
            bytes_received=meta["bytes_received"],
            speed_bps=transfer.speed_bps,
        )

    @staticmethod
    async def complete_transfer(
        db: AsyncSession,
        transfer_id: str,
    ) -> FileTransfer:
        """
        Complete transfer — verify checksum, assemble file, move to final location.
        Optionally index in media gallery.
        """
        transfer = await db.execute(
            select(FileTransfer).where(FileTransfer.id == transfer_id)
        )
        transfer = transfer.scalar_one_or_none()
        if not transfer:
            raise NotFoundError("FileTransfer", transfer_id)

        if transfer_id not in FileDropService._active_transfers:
            raise ValidationError("Transfer context not found")

        meta = FileDropService._active_transfers[transfer_id]
        temp_dir = Path(meta["temp_dir"])

        try:
            # Assemble chunks
            final_path = (
                Path(settings.upload_path)
                / f"transfer_{transfer_id}_{transfer.filename}"
            )
            final_path.parent.mkdir(parents=True, exist_ok=True)

            sha256 = hashlib.sha256()
            async with aiofiles.open(final_path, "wb") as out_f:
                for i in range(transfer.total_chunks):
                    chunk_path = temp_dir / f"chunk_{i:06d}"
                    if not chunk_path.exists():
                        raise ValidationError(f"Missing chunk {i}")

                    async with aiofiles.open(chunk_path, "rb") as in_f:
                        chunk_data = await in_f.read()
                        sha256.update(chunk_data)
                        await out_f.write(chunk_data)

            # Verify checksum
            computed = sha256.hexdigest()
            if computed != transfer.checksum:
                raise ValidationError(
                    f"Checksum mismatch: expected {transfer.checksum}, got {computed}"
                )

            # Mark as completed
            transfer.status = "completed"
            transfer.file_path = str(final_path)
            await db.commit()
            await db.refresh(transfer)

            # Cleanup temp directory
            try:
                import shutil
                shutil.rmtree(temp_dir, ignore_errors=True)
            except Exception as e:
                logger.warning("temp_cleanup_failed", error=str(e))

            del FileDropService._active_transfers[transfer_id]

            logger.info(
                "file_transfer_completed",
                transfer_id=transfer_id,
                filename=transfer.filename,
                final_path=str(final_path),
            )
            return transfer

        except Exception as e:
            transfer.status = "failed"
            transfer.error_message = str(e)
            await db.commit()
            logger.error(
                "file_transfer_completion_failed",
                transfer_id=transfer_id,
                error=str(e),
            )
            raise

    @staticmethod
    async def cancel_transfer(
        db: AsyncSession,
        transfer_id: str,
        user_id: str,
    ) -> None:
        """Cancel transfer and cleanup temp files."""
        transfer = await db.execute(
            select(FileTransfer).where(FileTransfer.id == transfer_id)
        )
        transfer = transfer.scalar_one_or_none()
        if not transfer:
            raise NotFoundError("FileTransfer", transfer_id)

        # Verify authorization
        if transfer.sender_id != user_id:
            raise ValidationError("Only the sender can cancel a transfer")

        # Cleanup
        if transfer_id in FileDropService._active_transfers:
            meta = FileDropService._active_transfers[transfer_id]
            temp_dir = Path(meta["temp_dir"])
            try:
                import shutil
                shutil.rmtree(temp_dir, ignore_errors=True)
            except Exception as e:
                logger.warning("temp_cleanup_failed", error=str(e))
            del FileDropService._active_transfers[transfer_id]

        transfer.status = "cancelled"
        await db.commit()

        logger.info("file_transfer_cancelled", transfer_id=transfer_id, user_id=user_id)

    @staticmethod
    async def get_transfer_status(db: AsyncSession, transfer_id: str) -> FileTransfer:
        """Get current transfer progress."""
        result = await db.execute(
            select(FileTransfer).where(FileTransfer.id == transfer_id)
        )
        transfer = result.scalar_one_or_none()
        if not transfer:
            raise NotFoundError("FileTransfer", transfer_id)
        return transfer

    @staticmethod
    async def list_active_transfers(
        db: AsyncSession,
        user_id: str,
        page: int = 1,
        per_page: int = 50,
    ) -> tuple[list[FileTransfer], int]:
        """List user's active transfers (sent or received)."""
        query = select(FileTransfer).where(
            and_(
                FileTransfer.status.in_(["pending", "uploading"]),
            )
        ).where(
            (FileTransfer.sender_id == user_id) | (FileTransfer.receiver_id == user_id)
        )

        # Count
        result = await db.execute(
            select(func.count(FileTransfer.id)).select_from(FileTransfer).where(
                and_(
                    FileTransfer.status.in_(["pending", "uploading"]),
                    (FileTransfer.sender_id == user_id) | (FileTransfer.receiver_id == user_id),
                )
            )
        )
        total = result.scalar() or 0

        # Paginate
        query = query.order_by(desc(FileTransfer.created_at)).offset(
            (page - 1) * per_page
        ).limit(per_page)

        result = await db.execute(query)
        transfers = result.scalars().all()

        return transfers, total

    @staticmethod
    async def create_shared_folder(
        db: AsyncSession,
        channel_id: str,
        user_id: str,
        name: str = "Shared Files",
        max_size_bytes: int = 1 * 1024 * 1024 * 1024,
    ) -> SharedFolder:
        """Create or get shared folder for channel."""
        # Check if exists
        result = await db.execute(
            select(SharedFolder).where(SharedFolder.channel_id == channel_id)
        )
        existing = result.scalar_one_or_none()
        if existing:
            return existing

        folder = SharedFolder(
            channel_id=channel_id,
            name=name,
            created_by=user_id,
            max_size_bytes=max_size_bytes,
        )
        db.add(folder)
        await db.commit()
        await db.refresh(folder)

        logger.info(
            "shared_folder_created",
            folder_id=folder.id,
            channel_id=channel_id,
            name=name,
        )
        return folder

    @staticmethod
    async def add_to_shared_folder(
        db: AsyncSession,
        folder_id: str,
        file_id: str,
        user_id: str,
        path_in_folder: str,
    ) -> SharedFolderFile:
        """Add file to shared folder."""
        folder = await db.execute(
            select(SharedFolder).where(SharedFolder.id == folder_id)
        )
        folder = folder.scalar_one_or_none()
        if not folder:
            raise NotFoundError("SharedFolder", folder_id)

        file_record = await db.execute(
            select(FileRecord).where(FileRecord.id == file_id)
        )
        file_record = file_record.scalar_one_or_none()
        if not file_record:
            raise NotFoundError("FileRecord", file_id)

        # Check size quota
        if folder.current_size_bytes + file_record.size_bytes > folder.max_size_bytes:
            raise ValidationError("Shared folder quota exceeded")

        # Add
        sf_file = SharedFolderFile(
            folder_id=folder_id,
            file_record_id=file_id,
            added_by=user_id,
            path_in_folder=path_in_folder,
        )
        db.add(sf_file)

        # Update folder size
        folder.current_size_bytes += file_record.size_bytes
        await db.commit()
        await db.refresh(sf_file, attribute_names=["file_record"])

        logger.info(
            "file_added_to_shared_folder",
            folder_id=folder_id,
            file_id=file_id,
            path=path_in_folder,
        )
        return sf_file

    @staticmethod
    async def list_shared_folder(
        db: AsyncSession,
        folder_id: str,
        path_prefix: str | None = None,
    ) -> list[SharedFolderFile]:
        """List files in shared folder, optionally by path prefix."""
        query = (
            select(SharedFolderFile)
            .where(SharedFolderFile.folder_id == folder_id)
            .options(selectinload(SharedFolderFile.file_record))
        )

        if path_prefix:
            query = query.where(SharedFolderFile.path_in_folder.startswith(path_prefix))

        query = query.order_by(SharedFolderFile.path_in_folder)
        result = await db.execute(query)
        return result.scalars().all()

    @staticmethod
    async def get_shared_folder(db: AsyncSession, channel_id: str) -> SharedFolder:
        """Get channel's shared folder."""
        result = await db.execute(
            select(SharedFolder).where(SharedFolder.channel_id == channel_id)
        )
        folder = result.scalar_one_or_none()
        if not folder:
            raise NotFoundError("SharedFolder for channel", channel_id)
        return folder

    @staticmethod
    async def remove_from_shared_folder(
        db: AsyncSession,
        folder_id: str,
        file_id: str,
        user_id: str,
    ) -> None:
        """Remove file from shared folder."""
        result = await db.execute(
            select(SharedFolderFile)
            .where(
                and_(
                    SharedFolderFile.folder_id == folder_id,
                    SharedFolderFile.file_record_id == file_id,
                )
            )
            .options(selectinload(SharedFolderFile.file_record))
        )
        sf_file = result.scalar_one_or_none()
        if not sf_file:
            raise NotFoundError("File in shared folder", file_id)

        # Get folder to update size
        folder = await db.execute(
            select(SharedFolder).where(SharedFolder.id == folder_id)
        )
        folder = folder.scalar_one_or_none()

        # Deduct size
        if folder:
            file_record = sf_file.file_record
            folder.current_size_bytes = max(
                0, folder.current_size_bytes - file_record.size_bytes
            )

        await db.delete(sf_file)
        await db.commit()

        logger.info(
            "file_removed_from_shared_folder",
            folder_id=folder_id,
            file_id=file_id,
        )

    @staticmethod
    async def cleanup_stale_transfers(
        db: AsyncSession,
        stale_hours: int = 24,
    ) -> int:
        """
        Background task: clean up stale pending/uploading transfers.
        Removes temp files and marks as failed.
        """
        from sqlalchemy import func as sql_func

        cutoff = datetime.now(timezone.utc) - timedelta(hours=stale_hours)

        result = await db.execute(
            select(FileTransfer).where(
                and_(
                    FileTransfer.status.in_(["pending", "uploading"]),
                    FileTransfer.created_at < cutoff,
                )
            )
        )
        stale = result.scalars().all()

        for transfer in stale:
            if transfer.id in FileDropService._active_transfers:
                meta = FileDropService._active_transfers[transfer.id]
                temp_dir = Path(meta["temp_dir"])
                try:
                    import shutil
                    shutil.rmtree(temp_dir, ignore_errors=True)
                except Exception as e:
                    logger.warning("temp_cleanup_failed", error=str(e))
                del FileDropService._active_transfers[transfer.id]

            transfer.status = "failed"
            transfer.error_message = "Cleanup: transfer expired"

        if stale:
            await db.commit()
            logger.info("stale_transfers_cleaned", count=len(stale))

        return len(stale)


# Import func for query building
from sqlalchemy import func
