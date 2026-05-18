"""
Resumable upload service.

Wire protocol
-------------
1.  POST  /api/files/resumable/init
    body: {filename, total_size, mime_type?, chunk_size?, expected_sha256?,
           channel_id?, metadata?}
    → {session_id, chunk_size, total_chunks, expires_at, missing_chunks: [...]}

2.  PUT   /api/files/resumable/{session_id}/chunk/{index}
    headers:
        Content-Range: bytes <offset>-<offset+size-1>/<total_size>
        X-Chunk-CRC32: <unsigned crc32 decimal>
        X-Chunk-SHA256: <hex>
    body: raw bytes (max chunk_size)
    → {index, received, total_chunks, bytes_received, progress_pct,
       next_expected: [indexes]}

3.  POST  /api/files/resumable/{session_id}/complete
    body: {expected_sha256?}
    → {file_id, size, sha256}

4.  GET   /api/files/resumable/{session_id}/status
    → {session_id, progress_pct, received_chunks, total_chunks, missing_chunks,
       expires_at, status}

5.  DELETE /api/files/resumable/{session_id}
    → aborts + GCs staging dir

Integrity model
---------------
* client sends CRC32 + SHA256 per chunk ; server recomputes both, rejects
  on mismatch so a byte flip on disk or on the wire is caught early
* after the final chunk arrives, concatenation is streamed into the final
  file and a global SHA256 is computed, then compared against
  expected_sha256 (if the client gave one)
* chunks live in ``<UPLOAD_DIR>/staging/<session_id>/<index>.part``; the
  permanent file is only created after the SHA256 check passes

Concurrency
-----------
* chunks for the same session can be uploaded in parallel (the client decides)
* the same chunk uploaded twice is idempotent — second copy overwrites the
  first and updates the CRC/SHA checks
* stale sessions are reaped by :func:`gc_expired_sessions`
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import uuid
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import case, delete, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.session import async_session_factory
from app.db.sqlite_tuning import ensure_durable_write
from app.models.file import FileRecord
from app.models.upload_session import (
    UPLOAD_SESSION_TTL_SECONDS,
    UploadChunk,
    UploadSession,
)

logger = get_logger(__name__)
settings = get_settings()


DEFAULT_CHUNK_SIZE = 1 << 18        # 256 KiB
MIN_CHUNK_SIZE = 16 * 1024          # 16 KiB
MAX_CHUNK_SIZE = 4 * 1024 * 1024    # 4 MiB


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(dt: datetime | None) -> datetime | None:
    """Normalize a DB-loaded datetime to tz-aware UTC.

    SQLite + SQLAlchemy's ``DateTime(timezone=True)`` round-trips as a naive
    datetime on the backend because SQLite has no native tz type. Compare
    with ``_utc_now()`` without this shim and you get TypeError.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _staging_root() -> Path:
    root = settings.upload_path / "staging"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _session_dir(session_id: str) -> Path:
    p = _staging_root() / session_id
    p.mkdir(parents=True, exist_ok=True)
    return p


def _chunk_path(session_id: str, index: int) -> Path:
    return _session_dir(session_id) / f"{index:08d}.part"


def _sanitize_filename(name: str) -> str:
    # Strip paths / null bytes; keep a reasonable character set.
    name = name.replace("\x00", "").strip()
    base = os.path.basename(name) or f"upload-{uuid.uuid4().hex}"
    return base[:512]


# ─────────────────────────────────────────────────────────────────────────────
# Public service API
# ─────────────────────────────────────────────────────────────────────────────

class ResumableUploadError(Exception):
    pass


class ChunkIntegrityError(ResumableUploadError):
    pass


class SessionNotFoundError(ResumableUploadError):
    pass


class SessionStateError(ResumableUploadError):
    pass


class ResumableUploadService:

    # ── Init ────────────────────────────────────────────────────────────────

    async def init_session(
        self,
        *,
        owner_id: str,
        filename: str,
        total_size: int,
        mime_type: str | None = None,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        expected_sha256: str | None = None,
        channel_id: str | None = None,
        metadata: dict | None = None,
    ) -> dict[str, Any]:
        if total_size <= 0:
            raise ResumableUploadError("total_size must be positive")
        if total_size > settings.max_upload_bytes:
            raise ResumableUploadError(
                f"file too large: {total_size} > {settings.max_upload_bytes}"
            )
        if chunk_size < MIN_CHUNK_SIZE or chunk_size > MAX_CHUNK_SIZE:
            raise ResumableUploadError(
                f"chunk_size must be within [{MIN_CHUNK_SIZE}, {MAX_CHUNK_SIZE}]"
            )

        # Whitelist extension (honors the existing security policy)
        ext = os.path.splitext(filename)[1].lower()
        if settings.allowed_ext_set and ext and ext not in settings.allowed_ext_set:
            raise ResumableUploadError(f"extension not allowed: {ext}")

        session_id = uuid.uuid4().hex
        total_chunks = (total_size + chunk_size - 1) // chunk_size
        expires_at = _utc_now() + timedelta(seconds=UPLOAD_SESSION_TTL_SECONDS)
        staging = _session_dir(session_id)

        row = UploadSession(
            id=session_id,
            owner_id=owner_id,
            channel_id=channel_id,
            filename=_sanitize_filename(filename),
            mime_type=mime_type,
            total_size=total_size,
            chunk_size=chunk_size,
            total_chunks=total_chunks,
            expected_sha256=expected_sha256,
            status="init",
            expires_at=expires_at,
            staging_path=str(staging),
            metadata_json=None if metadata is None else _dumps(metadata),
        )

        async with async_session_factory() as s:
            s.add(row)
            await ensure_durable_write(s)
            await s.commit()
            await s.refresh(row)

        logger.info(
            "upload_init",
            session_id=session_id,
            owner=owner_id,
            size=total_size,
            chunks=total_chunks,
        )
        return {
            "session_id": session_id,
            "chunk_size": chunk_size,
            "total_chunks": total_chunks,
            "expires_at": expires_at.isoformat(),
            "status": row.status,
            "missing_chunks": list(range(total_chunks)),
            # Explicit: a brand-new session has no received chunks. The
            # client expects this field for resume-on-init flows, so we
            # always emit it instead of relying on absence implying empty.
            "received": [],
        }

    # ── Upload chunk ────────────────────────────────────────────────────────

    async def put_chunk(
        self,
        *,
        session_id: str,
        owner_id: str,
        index: int,
        data: bytes,
        expected_crc32: int | None = None,
        expected_sha256: str | None = None,
        offset_hint: int | None = None,
    ) -> dict[str, Any]:
        async with async_session_factory() as s:
            row = await self._load_session(s, session_id, owner_id)

            if row.status not in {"init", "uploading"}:
                raise SessionStateError(f"session in terminal state: {row.status}")
            if _utc_now() > _as_utc(row.expires_at):
                await self._expire_locked(s, row)
                raise SessionStateError("session expired")
            if index < 0 or index >= row.total_chunks:
                raise ResumableUploadError(f"chunk index {index} out of range")

            expected_size = self._expected_chunk_size(row, index)
            if len(data) != expected_size:
                raise ResumableUploadError(
                    f"chunk {index} wrong size: {len(data)} != {expected_size}"
                )

            # Recompute checksums
            actual_crc32 = zlib.crc32(data) & 0xFFFFFFFF
            actual_sha256 = hashlib.sha256(data).hexdigest()

            if expected_crc32 is not None and actual_crc32 != (expected_crc32 & 0xFFFFFFFF):
                raise ChunkIntegrityError(
                    f"chunk {index} CRC32 mismatch: expected {expected_crc32}, got {actual_crc32}"
                )
            if expected_sha256 is not None and expected_sha256.lower() != actual_sha256:
                raise ChunkIntegrityError(
                    f"chunk {index} SHA256 mismatch"
                )

            offset = index * row.chunk_size
            if offset_hint is not None and offset_hint != offset:
                raise ResumableUploadError(
                    f"chunk offset hint mismatch: {offset_hint} != {offset}"
                )

            # Atomic file write — .tmp then rename
            target = _chunk_path(session_id, index)
            tmp = target.with_suffix(target.suffix + ".tmp")
            await asyncio.to_thread(_write_atomic, tmp, target, data)

            # ─────────────────────────────────────────────────────────────
            # Race-free upsert of the chunk row + session counters.
            #
            # Previous implementation did a read-modify-write on
            # row.received_chunks / row.bytes_received which lost updates
            # under concurrent chunk uploads (clients fan out 4-way in
            # parallel). We now:
            #   1) Atomically INSERT ... ON CONFLICT DO NOTHING the chunk row;
            #      rowcount == 1 tells us this was a fresh chunk.
            #   2) For new chunks, issue `col = col + N` UPDATE against the
            #      session — SQLite evaluates that at the storage layer so
            #      two concurrent writers both add their contribution.
            #   3) For duplicates, overwrite the chunk row and adjust the
            #      byte counter by the delta (still via `col = col + delta`).
            # ─────────────────────────────────────────────────────────────
            insert_stmt = (
                sqlite_insert(UploadChunk)
                .values(
                    session_id=session_id,
                    chunk_index=index,
                    offset=offset,
                    size=len(data),
                    crc32=actual_crc32,
                    sha256=actual_sha256,
                    verified=True,
                    received_at=_utc_now(),
                )
                .on_conflict_do_nothing(
                    index_elements=["session_id", "chunk_index"],
                )
            )
            ins_res = await s.execute(insert_stmt)
            was_insert = (ins_res.rowcount or 0) == 1

            if was_insert:
                await s.execute(
                    update(UploadSession)
                    .where(UploadSession.id == session_id)
                    .values(
                        received_chunks=UploadSession.received_chunks + 1,
                        bytes_received=UploadSession.bytes_received + len(data),
                        status=case(
                            (UploadSession.status == "init", "uploading"),
                            else_=UploadSession.status,
                        ),
                    )
                )
            else:
                # Re-upload — compute byte delta from the stored size, then
                # overwrite the chunk row and adjust counters atomically.
                existing_size = await s.scalar(
                    select(UploadChunk.size).where(
                        UploadChunk.session_id == session_id,
                        UploadChunk.chunk_index == index,
                    )
                )
                delta = len(data) - int(existing_size or 0)
                await s.execute(
                    update(UploadChunk)
                    .where(
                        UploadChunk.session_id == session_id,
                        UploadChunk.chunk_index == index,
                    )
                    .values(
                        offset=offset,
                        size=len(data),
                        crc32=actual_crc32,
                        sha256=actual_sha256,
                        verified=True,
                        received_at=_utc_now(),
                    )
                )
                if delta != 0:
                    await s.execute(
                        update(UploadSession)
                        .where(UploadSession.id == session_id)
                        .values(
                            bytes_received=UploadSession.bytes_received + delta,
                        )
                    )

            await s.commit()

            # Re-read counters as scalar columns to bypass the identity map —
            # `expire_on_commit=False` means `select(UploadSession)` would
            # return the stale mapped instance, masking the atomic update.
            fresh_row = (
                await s.execute(
                    select(
                        UploadSession.received_chunks,
                        UploadSession.bytes_received,
                        UploadSession.total_chunks,
                    ).where(UploadSession.id == session_id)
                )
            ).one_or_none()
            received_chunks = int(fresh_row[0]) if fresh_row else 0
            bytes_received = int(fresh_row[1]) if fresh_row else 0
            total_chunks = int(fresh_row[2]) if fresh_row else row.total_chunks
            progress_pct = (
                100.0 * received_chunks / total_chunks if total_chunks > 0 else 0.0
            )
            missing = await self._missing_chunks(s, session_id, total_chunks)

        return {
            "index": index,
            "received": received_chunks,
            "total_chunks": total_chunks,
            "bytes_received": bytes_received,
            "progress_pct": progress_pct,
            "next_expected": missing[:16],
            "complete_ready": received_chunks >= total_chunks,
        }

    # ── Status ──────────────────────────────────────────────────────────────

    async def get_status(self, session_id: str, owner_id: str) -> dict[str, Any]:
        async with async_session_factory() as s:
            row = await self._load_session(s, session_id, owner_id)
            missing = await self._missing_chunks(s, session_id, row.total_chunks)
            # Received = full chunk-index universe minus missing.
            # Clients (ResumableUploader.ts) consume this directly to avoid
            # having to invert a potentially sparse list on their end.
            missing_set = set(missing)
            received = [i for i in range(row.total_chunks) if i not in missing_set]
            return {
                "session_id": row.id,
                "filename": row.filename,
                "status": row.status,
                "total_chunks": row.total_chunks,
                "chunk_size": row.chunk_size,
                "received_chunks": row.received_chunks,
                "received": received,
                "bytes_received": row.bytes_received,
                "total_size": row.total_size,
                "progress_pct": row.progress_pct(),
                "expires_at": row.expires_at.isoformat(),
                "missing_chunks": missing,
                "file_id": row.file_record_id,
            }

    # ── Complete ────────────────────────────────────────────────────────────

    async def complete(
        self,
        session_id: str,
        owner_id: str,
        *,
        expected_sha256: str | None = None,
    ) -> dict[str, Any]:
        async with async_session_factory() as s:
            row = await self._load_session(s, session_id, owner_id)

            if row.status == "completed":
                return {
                    "session_id": row.id,
                    "file_id": row.file_record_id,
                    "sha256": row.computed_sha256,
                    "size": row.total_size,
                    "status": row.status,
                    "already_completed": True,
                }
            if row.status not in {"init", "uploading"}:
                raise SessionStateError(f"session in terminal state: {row.status}")
            if row.received_chunks < row.total_chunks:
                missing = await self._missing_chunks(s, session_id, row.total_chunks)
                raise SessionStateError(
                    f"not all chunks received: {row.received_chunks}/{row.total_chunks}, "
                    f"missing {len(missing)}"
                )

            # Concatenate chunks → final file and compute global SHA-256.
            final_name = f"{uuid.uuid4().hex}_{_sanitize_filename(row.filename)}"
            final_dir = settings.upload_path
            final_path = final_dir / final_name
            sha256_hex, size = await asyncio.to_thread(
                _concat_chunks_and_hash, session_id, row.total_chunks, final_path,
            )
            if size != row.total_size:
                # rollback — delete partial final file
                try:
                    final_path.unlink(missing_ok=True)
                except Exception:
                    pass
                raise ChunkIntegrityError(
                    f"size mismatch: assembled {size} != declared {row.total_size}"
                )

            expected = expected_sha256 or row.expected_sha256
            if expected and expected.lower() != sha256_hex:
                try:
                    final_path.unlink(missing_ok=True)
                except Exception:
                    pass
                raise ChunkIntegrityError(
                    f"final SHA256 mismatch: expected {expected}, got {sha256_hex}"
                )

            # Create permanent FileRecord
            file_rec = FileRecord(
                uploader_id=owner_id,
                channel_id=row.channel_id,
                original_name=row.filename,
                stored_name=final_name,
                mime_type=row.mime_type or "application/octet-stream",
                size_bytes=size,
                storage_path=str(final_path),
                checksum_sha256=sha256_hex,
            )
            s.add(file_rec)
            await s.flush()

            row.status = "completed"
            row.completed_at = _utc_now()
            row.computed_sha256 = sha256_hex
            row.file_record_id = file_rec.id

            await ensure_durable_write(s)
            await s.commit()

        # Remove staging on success (best-effort)
        try:
            await asyncio.to_thread(shutil.rmtree, _session_dir(session_id), True)
        except Exception:
            pass

        logger.info(
            "upload_completed",
            session_id=session_id, file_id=file_rec.id, size=size, sha256=sha256_hex,
        )
        return {
            "session_id": session_id,
            "file_id": file_rec.id,
            "size": size,
            "sha256": sha256_hex,
            "status": "completed",
        }

    # ── Abort ───────────────────────────────────────────────────────────────

    async def abort(self, session_id: str, owner_id: str) -> None:
        async with async_session_factory() as s:
            row = await self._load_session(s, session_id, owner_id)
            row.status = "aborted"
            row.failed_at = _utc_now()
            row.failure_reason = "client abort"
            await s.commit()

        try:
            await asyncio.to_thread(shutil.rmtree, _session_dir(session_id), True)
        except Exception:
            pass
        logger.info("upload_aborted", session_id=session_id, owner=owner_id)

    # ── GC ──────────────────────────────────────────────────────────────────

    async def gc_expired_sessions(self) -> int:
        """Delete expired sessions + their staging dirs. Returns count removed.

        Three-phase sweep:
          1. Flip in-flight sessions past TTL to ``expired`` and nuke their
             staging dirs.
          2. Remove staging dirs for terminal sessions (completed / aborted /
             failed / expired) older than a grace window — keeps the parts
             around briefly in case the client retries complete() after a
             transient glitch.
          3. Delete orphan staging dirs on disk with no matching DB row
             (e.g. leftover from a crash during init before the row
             committed).
        """
        now = _utc_now()
        total_cleaned = 0
        terminal_grace_seconds = 6 * 3600  # keep terminal parts 6h

        # ── Phase 1: TTL expiry for live sessions ────────────────────────
        async with async_session_factory() as s:
            rows = (await s.scalars(
                select(UploadSession).where(
                    UploadSession.expires_at < now,
                    UploadSession.status.in_(["init", "uploading"]),
                )
            )).all()
            for r in rows:
                r.status = "expired"
                r.failed_at = now
                r.failure_reason = "ttl"
            if rows:
                await s.commit()

        for r in rows:
            try:
                await asyncio.to_thread(shutil.rmtree, _session_dir(r.id), True)
            except Exception:
                pass
        total_cleaned += len(rows)
        if rows:
            logger.info("upload_gc_expired", count=len(rows))

        # ── Phase 2: sweep terminal-state parts past the grace window ────
        grace_cutoff = now - timedelta(seconds=terminal_grace_seconds)
        async with async_session_factory() as s:
            terminal_rows = (await s.scalars(
                select(UploadSession).where(
                    UploadSession.status.in_(
                        ["completed", "aborted", "failed", "expired"]
                    ),
                    UploadSession.updated_at < grace_cutoff,
                )
            )).all()
        parts_cleaned = 0
        for r in terminal_rows:
            d = _staging_root() / r.id
            if d.exists():
                try:
                    await asyncio.to_thread(shutil.rmtree, d, True)
                    parts_cleaned += 1
                except Exception:
                    pass
        if parts_cleaned:
            logger.info("upload_gc_terminal_parts", count=parts_cleaned)
            total_cleaned += parts_cleaned

        # ── Phase 3: orphan staging dirs (no DB row) ─────────────────────
        try:
            known_ids: set[str] = set()
            async with async_session_factory() as s:
                ids = (await s.scalars(select(UploadSession.id))).all()
                known_ids = set(ids)
            root = _staging_root()
            if root.exists():
                for entry in await asyncio.to_thread(list, root.iterdir()):
                    if not entry.is_dir():
                        continue
                    if entry.name in known_ids:
                        continue
                    # Require age so we don't race an init() that just
                    # created the dir but hasn't committed the row yet.
                    try:
                        age = now.timestamp() - entry.stat().st_mtime
                    except OSError:
                        continue
                    if age < 300:  # 5 min grace
                        continue
                    try:
                        await asyncio.to_thread(shutil.rmtree, entry, True)
                        total_cleaned += 1
                        logger.info("upload_gc_orphan_dir", session_id=entry.name)
                    except Exception:
                        pass
        except Exception as e:
            logger.warning("upload_gc_orphan_scan_failed", error=str(e))

        return total_cleaned

    # ── Internals ───────────────────────────────────────────────────────────

    async def _load_session(
        self, s: AsyncSession, session_id: str, owner_id: str
    ) -> UploadSession:
        row = await s.get(UploadSession, session_id)
        if row is None:
            raise SessionNotFoundError(f"upload session {session_id} not found")
        if row.owner_id != owner_id:
            raise SessionNotFoundError("session does not belong to caller")
        return row

    def _expected_chunk_size(self, row: UploadSession, index: int) -> int:
        if index < row.total_chunks - 1:
            return row.chunk_size
        # last chunk may be shorter
        return row.total_size - (row.total_chunks - 1) * row.chunk_size

    async def _missing_chunks(
        self, s: AsyncSession, session_id: str, total_chunks: int
    ) -> list[int]:
        present = set((await s.scalars(
            select(UploadChunk.chunk_index).where(UploadChunk.session_id == session_id)
        )).all())
        return [i for i in range(total_chunks) if i not in present]

    async def _expire_locked(self, s: AsyncSession, row: UploadSession) -> None:
        row.status = "expired"
        row.failed_at = _utc_now()
        row.failure_reason = "ttl"
        await s.commit()


# ── Sync file helpers (run via asyncio.to_thread) ──────────────────────────

def _write_atomic(tmp: Path, target: Path, data: bytes) -> None:
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        try:
            os.fsync(f.fileno())
        except Exception:
            pass
    os.replace(tmp, target)


def _concat_chunks_and_hash(
    session_id: str, total_chunks: int, final_path: Path,
) -> tuple[str, int]:
    """Stream every chunk into ``final_path`` and return (sha256_hex, size).
    Cleans up the .assembly temp file on any failure path so a disk-full /
    permission error doesn't leak partial uploads into staging."""
    h = hashlib.sha256()
    size = 0
    final_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = final_path.with_suffix(final_path.suffix + ".assembly")
    try:
        with open(tmp, "wb") as out:
            for i in range(total_chunks):
                path = _chunk_path(session_id, i)
                if not path.exists():
                    raise ChunkIntegrityError(f"chunk {i} missing from staging")
                with open(path, "rb") as inp:
                    while True:
                        buf = inp.read(1 << 16)
                        if not buf:
                            break
                        h.update(buf)
                        out.write(buf)
                        size += len(buf)
            out.flush()
            try:
                os.fsync(out.fileno())
            except Exception:
                pass
        os.replace(tmp, final_path)
        return h.hexdigest(), size
    except BaseException:
        # Best-effort cleanup of the assembly temp file. Catches
        # BaseException so KeyboardInterrupt mid-assembly also tidies.
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        raise


# ── Tiny JSON helpers (avoid circular imports) ─────────────────────────────

def _dumps(obj: Any) -> str:
    import json
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


# Singleton
resumable_upload_service = ResumableUploadService()
