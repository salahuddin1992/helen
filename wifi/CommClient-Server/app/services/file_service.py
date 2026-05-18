"""
File upload and storage service.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path

import aiofiles
from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.core.mime_sniffer import HEAD_BYTES_REQUIRED, validate_upload
from app.models.file import FileRecord

logger = get_logger(__name__)
settings = get_settings()


class FileService:

    @staticmethod
    async def upload_file(
        db: AsyncSession,
        uploader_id: str,
        file: UploadFile,
        channel_id: str | None = None,
    ) -> FileRecord:
        """Upload and store a file. Returns metadata record.

        Streams the UploadFile to disk in 1 MB chunks while incrementally
        hashing + sniffing MIME from the first HEAD_BYTES_REQUIRED bytes.
        This keeps memory constant regardless of file size — a 50 GB upload
        uses the same RSS as a 50 KB one.
        """
        # Path-traversal guard on the client-supplied filename. The
        # frontend should already strip slashes / drive letters, but
        # we never trust untrusted input here. We reject absolute
        # paths, parent-directory references, and Windows drive
        # specifiers — these would otherwise let an attacker escape
        # the upload directory once the filename hits any downstream
        # logging / archive / preview that joins paths naively.
        raw_name = file.filename or ""
        sanitized_name = Path(raw_name).name  # strip any directory component
        if (sanitized_name != raw_name) or ".." in sanitized_name or "\x00" in sanitized_name:
            logger.warning(
                "file_upload_rejected_path_traversal",
                filename=raw_name, uploader_id=uploader_id,
            )
            raise ValidationError("Invalid filename — path components not allowed")

        # Validate extension. We enforce TWO gates:
        #   1. allowed_ext_set (opt-in allowlist) — admin-configured per env
        #   2. DANGEROUS_EXTENSIONS (always-deny) — common Windows/Unix
        #      executable surfaces that should never be storable as
        #      "files" regardless of allowlist config. Catches both
        #      naive uploads and clients that bypass extension filtering.
        DANGEROUS_EXTENSIONS = frozenset({
            ".exe", ".bat", ".cmd", ".com", ".pif", ".scr", ".lnk",
            ".vbs", ".vbe", ".js", ".jse", ".ws", ".wsf", ".wsh",
            ".ps1", ".ps1xml", ".ps2", ".ps2xml", ".psc1", ".psc2",
            ".msh", ".msh1", ".msh2", ".mshxml", ".msh1xml", ".msh2xml",
            ".reg", ".msi", ".msp", ".hta", ".cpl", ".jar",
            ".sh", ".bash", ".csh", ".zsh",  # shell scripts
        })
        ext = Path(sanitized_name).suffix.lower()
        if ext in DANGEROUS_EXTENSIONS:
            logger.warning(
                "file_upload_rejected_dangerous_ext",
                filename=sanitized_name, ext=ext, uploader_id=uploader_id,
            )
            raise ValidationError(
                f"File type '{ext}' is not allowed for security reasons",
            )
        if settings.allowed_ext_set and ext and ext not in settings.allowed_ext_set:
            raise ValidationError(f"File type '{ext}' is not allowed")

        # Generate unique stored name up front so we can stream straight to it.
        file_uuid = uuid.uuid4().hex
        stored_name = f"{file_uuid}{ext}"
        storage_path = settings.upload_path / stored_name

        STREAM_CHUNK = 1 << 20  # 1 MiB
        hasher = hashlib.sha256()
        head = bytearray()
        size = 0
        max_bytes = settings.max_upload_bytes

        try:
            async with aiofiles.open(storage_path, "wb") as out:
                while True:
                    chunk = await file.read(STREAM_CHUNK)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > max_bytes:
                        raise ValidationError(
                            f"File too large ({size} bytes). Max: {max_bytes} bytes"
                        )
                    if len(head) < HEAD_BYTES_REQUIRED:
                        needed = HEAD_BYTES_REQUIRED - len(head)
                        head.extend(chunk[:needed])
                    hasher.update(chunk)
                    await out.write(chunk)
        except Exception:
            # Anything goes wrong mid-stream (disk full, MIME rejection below,
            # oversized, client disconnect) → the half-written file is garbage.
            try:
                storage_path.unlink(missing_ok=True)
            except Exception:
                pass
            raise

        # ── Content sniffing on the captured head ───────────────────────
        # Defense-in-depth: reject spoofed payloads (e.g. .exe renamed to
        # .jpg) regardless of claimed content_type.
        try:
            canonical_mime, mime_warnings = validate_upload(
                head=bytes(head),
                claimed_mime=file.content_type,
                ext=ext,
            )
        except ValueError as e:
            try:
                storage_path.unlink(missing_ok=True)
            except Exception:
                pass
            logger.warning(
                "file_upload_rejected_dangerous",
                filename=file.filename,
                claimed_mime=file.content_type,
                reason=str(e),
                uploader_id=uploader_id,
            )
            raise ValidationError(str(e))

        if mime_warnings:
            logger.info(
                "file_upload_mime_warnings",
                filename=file.filename,
                warnings=mime_warnings,
                canonical_mime=canonical_mime,
            )

        checksum = hasher.hexdigest()

        # Generate thumbnail for images / videos — use the sniffed MIME,
        # not the client-supplied one, so spoofed claims can't trick ffmpeg.
        thumbnail_path = None
        ctype = canonical_mime.lower()
        try:
            if ctype.startswith("image/"):
                thumbnail_path = await FileService._generate_image_thumbnail(
                    storage_path, file_uuid
                )
            elif ctype.startswith("video/"):
                thumbnail_path = await FileService._generate_video_thumbnail(
                    storage_path, file_uuid
                )
        except Exception as e:
            logger.warning("thumbnail_failed", error=str(e), mime=ctype)

        record = FileRecord(
            uploader_id=uploader_id,
            channel_id=channel_id,
            original_name=sanitized_name or "unknown",
            stored_name=stored_name,
            # Use sniffed MIME as source of truth — prevents clients from
            # smuggling a dangerous type past downstream consumers.
            mime_type=canonical_mime or "application/octet-stream",
            size_bytes=size,
            storage_path=str(storage_path),
            thumbnail_path=str(thumbnail_path) if thumbnail_path else None,
            checksum_sha256=checksum,
        )
        db.add(record)
        await db.flush()  # get record.id before acceptance bootstrap

        # Bootstrap per-recipient acceptance rows for group files so every
        # channel member has a tracked state from the moment of upload.
        if channel_id:
            try:
                from app.services.file_acceptance_service import (
                    FileAcceptanceService,
                )
                await FileAcceptanceService.ensure_rows_for_channel_file(
                    db,
                    file_id=record.id,
                    channel_id=channel_id,
                    uploader_id=uploader_id,
                )
            except Exception as e:
                # Bootstrap is best-effort — rows can be backfilled on
                # first recipient action. Never block the upload.
                logger.warning(
                    "file_acceptance_bootstrap_failed",
                    file_id=record.id, channel_id=channel_id, error=str(e),
                )

        await db.commit()
        await db.refresh(record)

        logger.info("file_uploaded", file_id=record.id, name=file.filename, size=size)
        return record

    @staticmethod
    async def get_file(db: AsyncSession, file_id: str) -> FileRecord:
        result = await db.execute(select(FileRecord).where(FileRecord.id == file_id))
        record = result.scalar_one_or_none()
        if not record:
            raise NotFoundError("File", file_id)
        return record

    @staticmethod
    async def delete_file(
        db: AsyncSession,
        file_id: str,
        user_id: str,
    ) -> None:
        record = await FileService.get_file(db, file_id)
        if record.uploader_id != user_id:
            raise ValidationError("You can only delete your own files")

        # Remove from disk
        try:
            if os.path.exists(record.storage_path):
                os.remove(record.storage_path)
            if record.thumbnail_path and os.path.exists(record.thumbnail_path):
                os.remove(record.thumbnail_path)
        except OSError as e:
            logger.warning("file_delete_disk_error", error=str(e))

        await db.delete(record)
        await db.commit()
        logger.info("file_deleted", file_id=file_id)

    @staticmethod
    async def _generate_image_thumbnail(
        source_path: Path,
        file_uuid: str,
        max_dim: int = 320,
        output_dir: Path | None = None,
    ) -> Path | None:
        """
        Generate a thumbnail for an image using Pillow.
        - Honours EXIF orientation so portrait photos aren't rotated.
        - Preserves aspect ratio inside (max_dim x max_dim).
        - Outputs JPEG quality 82 with progressive encoding.
        """
        try:
            from PIL import Image, ImageOps

            thumb_name = f"{file_uuid}_thumb.jpg"
            thumb_path = (output_dir or settings.upload_path) / thumb_name

            with Image.open(source_path) as img:
                # Apply EXIF rotation (e.g. iPhone portrait shots)
                img = ImageOps.exif_transpose(img)
                img.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
                # Drop alpha so JPEG save works for PNG/WEBP
                if img.mode in ("RGBA", "LA", "P"):
                    bg = Image.new("RGB", img.size, (255, 255, 255))
                    bg.paste(img, mask=img.split()[-1] if img.mode in ("RGBA", "LA") else None)
                    img = bg
                else:
                    img = img.convert("RGB")
                img.save(thumb_path, "JPEG", quality=82, optimize=True, progressive=True)

            return thumb_path
        except ImportError:
            logger.warning("pillow_not_installed")
            return None
        except Exception as e:
            logger.warning("image_thumbnail_failed", error=str(e), source=str(source_path))
            return None

    @staticmethod
    async def _generate_video_thumbnail(
        source_path: Path,
        file_uuid: str,
        max_dim: int = 320,
        output_dir: Path | None = None,
    ) -> Path | None:
        """
        Generate a thumbnail for a video by extracting a representative frame.
        - Probes duration with ffprobe and seeks to ~10% in (or 1s for very short clips).
        - Falls back to first frame if probe fails.
        - Requires the `ffmpeg` and `ffprobe` binaries on PATH.
        """
        import asyncio
        import shutil

        ffmpeg = shutil.which("ffmpeg")
        ffprobe = shutil.which("ffprobe")
        if not ffmpeg:
            logger.info("video_thumbnail_skipped_no_ffmpeg")
            return None

        thumb_name = f"{file_uuid}_thumb.jpg"
        thumb_path = (output_dir or settings.upload_path) / thumb_name

        # Probe duration so we can pick a meaningful seek time
        seek_seconds = "1.0"
        if ffprobe:
            try:
                proc = await asyncio.create_subprocess_exec(
                    ffprobe,
                    "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    str(source_path),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=8)
                duration_str = stdout.decode("ascii", errors="ignore").strip()
                if duration_str:
                    duration = float(duration_str)
                    # Seek 10% in, but at least 1s and at most 10s
                    seek = max(1.0, min(duration * 0.1, 10.0))
                    if duration > 0.5:
                        seek_seconds = f"{seek:.2f}"
            except Exception as e:
                logger.debug("ffprobe_failed", error=str(e))

        try:
            proc = await asyncio.create_subprocess_exec(
                ffmpeg,
                "-y",
                "-ss", seek_seconds,
                "-i", str(source_path),
                "-vframes", "1",
                "-vf", f"scale='if(gt(a,1),{max_dim},-2)':'if(gt(a,1),-2,{max_dim})'",
                "-q:v", "3",
                str(thumb_path),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
            except asyncio.TimeoutError:
                proc.kill()
                logger.warning("video_thumbnail_timeout", source=str(source_path))
                return None

            if proc.returncode == 0 and thumb_path.exists() and thumb_path.stat().st_size > 0:
                return thumb_path

            logger.warning(
                "video_thumbnail_ffmpeg_failed",
                rc=proc.returncode,
                stderr=stderr.decode("utf-8", errors="ignore")[-200:],
            )
            return None
        except Exception as e:
            logger.warning("video_thumbnail_error", error=str(e))
            return None
