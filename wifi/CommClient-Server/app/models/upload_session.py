"""
Resumable upload session + per-chunk tracking.

Design
------
The classic single-POST upload dies on any network blip. This table tracks:

* a global ``UploadSession`` per file-to-upload
* one ``UploadChunk`` row per fixed-size slice with a SHA-256 and CRC-32
* the session carries a final expected SHA-256 + size so we can verify
  end-to-end integrity on finalize

The file data itself lands in ``<UPLOAD_DIR>/staging/<session_id>/NNNNNN.part``
(one file per chunk) and is concatenated on ``complete``. Staging is separate
from the permanent ``files/`` tree so a crashed upload never leaves a corrupt
file visible.

Why not store chunk bytes in DB? SQLite blob I/O fights with the WAL writer
during group voice calls. Filesystem writes are ~20× faster and reuse the
same LAN mount.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, utc_now


UPLOAD_SESSION_TTL_SECONDS = 60 * 60 * 24 * 7  # 7 days — long offline tolerance


class UploadSession(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "upload_sessions"

    owner_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    channel_id: Mapped[str | None] = mapped_column(
        String(32),
        ForeignKey("channels.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    total_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    chunk_size: Mapped[int] = mapped_column(Integer, nullable=False, default=1 << 18)  # 256 KiB default
    total_chunks: Mapped[int] = mapped_column(Integer, nullable=False)

    # End-to-end checksum that the client declared at init; verified on complete.
    expected_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Server-computed once all chunks are present + concatenated.
    computed_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)

    received_chunks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    bytes_received: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="init", index=True,
    )  # init|uploading|completed|failed|expired|aborted

    # Final file record id after finalize (FK to files.id — table name is "files").
    file_record_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("files.id", ondelete="SET NULL"), nullable=True,
    )

    # TTL — reaper task deletes expired sessions from disk + DB.
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Where chunks are staged on disk (relative to UPLOAD_DIR).
    staging_path: Mapped[str] = mapped_column(String(512), nullable=False)

    # Free-form — e2ee encryption envelope, transport hints, etc.
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    chunks: Mapped[list["UploadChunk"]] = relationship(
        "UploadChunk",
        back_populates="session",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    __table_args__ = (
        Index("ix_upload_sessions_owner_status", "owner_id", "status"),
        Index("ix_upload_sessions_expires_status", "expires_at", "status"),
    )

    def progress_pct(self) -> float:
        if self.total_chunks <= 0:
            return 0.0
        return 100.0 * self.received_chunks / self.total_chunks

    def is_complete(self) -> bool:
        return self.received_chunks >= self.total_chunks

    def __repr__(self) -> str:  # pragma: no cover
        return f"<UploadSession {self.id[:8]} {self.filename} {self.progress_pct():.1f}%>"


class UploadChunk(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "upload_chunks"

    session_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("upload_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    offset: Mapped[int] = mapped_column(BigInteger, nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False)

    crc32: Mapped[int] = mapped_column(BigInteger, nullable=False)        # unsigned CRC-32
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    session: Mapped[UploadSession] = relationship("UploadSession", back_populates="chunks")

    __table_args__ = (
        UniqueConstraint("session_id", "chunk_index", name="uq_upload_chunk_index"),
        Index("ix_upload_chunk_session_idx", "session_id", "chunk_index"),
    )
