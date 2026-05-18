"""
Unit tests for ResumableUploadService.

Coverage
--------
* init → put_chunk (single) → complete happy path
* duplicate chunk is idempotent (byte counters don't double-count)
* CRC32 mismatch is rejected with ChunkIntegrityError
* SHA256 mismatch is rejected with ChunkIntegrityError
* out-of-range chunk index rejected
* parallel put_chunk for different indexes is race-free
  (the counter equals the number of distinct chunks, not some racey value)
* expired session rejects subsequent chunk uploads
* status returns correct missing_chunks list

These tests hit the DB path directly — no FastAPI layer — so they exercise
the `ON CONFLICT DO NOTHING` + atomic `col = col + N` machinery that
replaced the read-modify-write race in put_chunk.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import update

from app.db.base import Base
from app.db.session import async_session_factory, engine
from app.models.upload_session import UploadSession
from app.models.user import User
from app.core.security import hash_password
from app.services.resumable_upload_service import (
    ChunkIntegrityError,
    MIN_CHUNK_SIZE,
    ResumableUploadError,
    ResumableUploadService,
    SessionStateError,
)


pytestmark = pytest.mark.asyncio


# ─────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
async def module_engine():
    """Module-scoped engine — create schema once, share across the file."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine


@pytest.fixture
async def owner(module_engine):
    """Insert a test user to satisfy FK on upload_sessions.owner_id."""
    async with async_session_factory() as s:
        u = User(
            id=uuid.uuid4().hex,
            username=f"uploader-{uuid.uuid4().hex[:8]}",
            display_name="Uploader",
            password_hash=hash_password("x"),
            status="online",
        )
        s.add(u)
        await s.commit()
        await s.refresh(u)
        return u


@pytest.fixture
def service() -> ResumableUploadService:
    return ResumableUploadService()


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────


def _chunk_headers(data: bytes) -> tuple[int, str]:
    return zlib.crc32(data) & 0xFFFFFFFF, hashlib.sha256(data).hexdigest()


# ─────────────────────────────────────────────────────────────────────
# Happy path
# ─────────────────────────────────────────────────────────────────────


async def test_init_put_complete_roundtrip(service, owner, tmp_path):
    """Single chunk init → put → complete should produce a FileRecord."""
    data = b"A" * MIN_CHUNK_SIZE
    sha = hashlib.sha256(data).hexdigest()

    init = await service.init_session(
        owner_id=owner.id,
        filename="hello.bin",
        total_size=len(data),
        chunk_size=MIN_CHUNK_SIZE,
        expected_sha256=sha,
    )
    assert init["total_chunks"] == 1
    assert init["status"] == "init"

    crc, chunk_sha = _chunk_headers(data)
    res = await service.put_chunk(
        session_id=init["session_id"],
        owner_id=owner.id,
        index=0,
        data=data,
        expected_crc32=crc,
        expected_sha256=chunk_sha,
    )
    assert res["received"] == 1
    assert res["complete_ready"] is True

    completed = await service.complete(init["session_id"], owner.id, expected_sha256=sha)
    assert completed["file_id"]
    assert completed["sha256"] == sha
    assert completed["size"] == len(data)


# ─────────────────────────────────────────────────────────────────────
# Idempotency + counter sanity
# ─────────────────────────────────────────────────────────────────────


async def test_duplicate_chunk_does_not_double_count(service, owner):
    """Re-uploading the same chunk must not inflate received_chunks or bytes_received."""
    data = b"B" * MIN_CHUNK_SIZE
    init = await service.init_session(
        owner_id=owner.id,
        filename="dup.bin",
        total_size=len(data) * 2,
        chunk_size=MIN_CHUNK_SIZE,
    )
    crc, sha = _chunk_headers(data)

    r1 = await service.put_chunk(
        session_id=init["session_id"], owner_id=owner.id, index=0,
        data=data, expected_crc32=crc, expected_sha256=sha,
    )
    r2 = await service.put_chunk(
        session_id=init["session_id"], owner_id=owner.id, index=0,
        data=data, expected_crc32=crc, expected_sha256=sha,
    )
    assert r1["received"] == 1
    assert r2["received"] == 1
    assert r2["bytes_received"] == len(data)


async def test_parallel_distinct_chunks_race_free(service, owner):
    """
    Fire put_chunk for 8 distinct indexes concurrently. Under the old
    read-modify-write pattern this would sporadically lose updates to
    received_chunks; with atomic UPDATE x = x + 1 it should always land
    at exactly 8.
    """
    chunk_size = MIN_CHUNK_SIZE
    total_chunks = 8
    init = await service.init_session(
        owner_id=owner.id,
        filename="parallel.bin",
        total_size=chunk_size * total_chunks,
        chunk_size=chunk_size,
    )
    sid = init["session_id"]

    async def put(i: int):
        data = bytes([i & 0xFF]) * chunk_size
        crc, sha = _chunk_headers(data)
        return await service.put_chunk(
            session_id=sid, owner_id=owner.id, index=i,
            data=data, expected_crc32=crc, expected_sha256=sha,
        )

    # Fan out all 8 puts concurrently
    results = await asyncio.gather(*(put(i) for i in range(total_chunks)))
    final_received = max(r["received"] for r in results)
    assert final_received == total_chunks

    status = await service.get_status(sid, owner.id)
    assert status["received_chunks"] == total_chunks
    assert status["bytes_received"] == chunk_size * total_chunks


# ─────────────────────────────────────────────────────────────────────
# Integrity checks
# ─────────────────────────────────────────────────────────────────────


async def test_crc32_mismatch_rejected(service, owner):
    data = b"C" * MIN_CHUNK_SIZE
    init = await service.init_session(
        owner_id=owner.id, filename="bad_crc.bin", total_size=len(data), chunk_size=MIN_CHUNK_SIZE,
    )
    _, sha = _chunk_headers(data)
    with pytest.raises(ChunkIntegrityError):
        await service.put_chunk(
            session_id=init["session_id"], owner_id=owner.id, index=0,
            data=data, expected_crc32=0xDEADBEEF, expected_sha256=sha,
        )


async def test_sha256_mismatch_rejected(service, owner):
    data = b"D" * MIN_CHUNK_SIZE
    init = await service.init_session(
        owner_id=owner.id, filename="bad_sha.bin", total_size=len(data), chunk_size=MIN_CHUNK_SIZE,
    )
    crc, _ = _chunk_headers(data)
    with pytest.raises(ChunkIntegrityError):
        await service.put_chunk(
            session_id=init["session_id"], owner_id=owner.id, index=0,
            data=data, expected_crc32=crc, expected_sha256="0" * 64,
        )


async def test_chunk_index_out_of_range_rejected(service, owner):
    data = b"E" * MIN_CHUNK_SIZE
    init = await service.init_session(
        owner_id=owner.id, filename="oor.bin", total_size=len(data), chunk_size=MIN_CHUNK_SIZE,
    )
    crc, sha = _chunk_headers(data)
    with pytest.raises(ResumableUploadError):
        await service.put_chunk(
            session_id=init["session_id"], owner_id=owner.id, index=99,
            data=data, expected_crc32=crc, expected_sha256=sha,
        )


async def test_wrong_chunk_size_rejected(service, owner):
    init = await service.init_session(
        owner_id=owner.id, filename="wrong_size.bin", total_size=MIN_CHUNK_SIZE * 2, chunk_size=MIN_CHUNK_SIZE,
    )
    bad = b"F" * 128
    crc, sha = _chunk_headers(bad)
    with pytest.raises(ResumableUploadError):
        await service.put_chunk(
            session_id=init["session_id"], owner_id=owner.id, index=0,
            data=bad, expected_crc32=crc, expected_sha256=sha,
        )


# ─────────────────────────────────────────────────────────────────────
# Session lifecycle
# ─────────────────────────────────────────────────────────────────────


async def test_expired_session_rejects_put(service, owner):
    init = await service.init_session(
        owner_id=owner.id, filename="expired.bin", total_size=MIN_CHUNK_SIZE, chunk_size=MIN_CHUNK_SIZE,
    )
    # Force the session into the past.
    async with async_session_factory() as s:
        await s.execute(
            update(UploadSession)
            .where(UploadSession.id == init["session_id"])
            .values(expires_at=datetime.now(timezone.utc) - timedelta(minutes=1))
        )
        await s.commit()

    data = b"G" * MIN_CHUNK_SIZE
    crc, sha = _chunk_headers(data)
    with pytest.raises(SessionStateError):
        await service.put_chunk(
            session_id=init["session_id"], owner_id=owner.id, index=0,
            data=data, expected_crc32=crc, expected_sha256=sha,
        )


async def test_status_reports_missing_chunks(service, owner):
    chunk_size = MIN_CHUNK_SIZE
    total_chunks = 4
    init = await service.init_session(
        owner_id=owner.id, filename="missing.bin",
        total_size=chunk_size * total_chunks, chunk_size=chunk_size,
    )
    data = b"H" * chunk_size
    crc, sha = _chunk_headers(data)

    # Upload only chunks 0 and 2
    await service.put_chunk(
        session_id=init["session_id"], owner_id=owner.id, index=0,
        data=data, expected_crc32=crc, expected_sha256=sha,
    )
    await service.put_chunk(
        session_id=init["session_id"], owner_id=owner.id, index=2,
        data=data, expected_crc32=crc, expected_sha256=sha,
    )

    status = await service.get_status(init["session_id"], owner.id)
    assert status["received_chunks"] == 2
    assert sorted(status["missing_chunks"]) == [1, 3]
