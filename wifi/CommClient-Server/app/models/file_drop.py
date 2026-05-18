"""
File drop models — chunked uploads, transfers, and shared folders.
Supports DM and group file transfers with progress tracking.
"""

from __future__ import annotations

from sqlalchemy import BigInteger, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class FileTransfer(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Chunked file transfer tracking — tracks upload progress and completion."""
    __tablename__ = "file_transfers"

    sender_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    receiver_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=True,
    )
    channel_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("channels.id", ondelete="CASCADE"), nullable=True,
    )
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    chunk_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    total_chunks: Mapped[int] = mapped_column(nullable=False)
    received_chunks: Mapped[int] = mapped_column(default=0, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending",
    )  # "pending", "uploading", "completed", "failed", "cancelled"
    file_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    speed_bps: Mapped[float | None] = mapped_column(nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Relationships
    sender: Mapped["User"] = relationship("User", foreign_keys=[sender_id])
    receiver: Mapped["User | None"] = relationship("User", foreign_keys=[receiver_id])
    channel: Mapped["Channel | None"] = relationship("Channel", foreign_keys=[channel_id])

    def __repr__(self) -> str:
        return f"<FileTransfer {self.filename} ({self.status}) ({self.id[:8]})>"


class SharedFolder(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Shared folder per channel — persistent shared space."""
    __tablename__ = "shared_folders"
    __table_args__ = (UniqueConstraint("channel_id", name="uq_shared_folder_per_channel"),)

    channel_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("channels.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    name: Mapped[str] = mapped_column(String(256), nullable=False, default="Shared Files")
    created_by: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )
    max_size_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=1 * 1024 * 1024 * 1024
    )  # 1GB default
    current_size_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    # Relationships
    channel: Mapped["Channel"] = relationship("Channel", foreign_keys=[channel_id])
    created_by_user: Mapped["User | None"] = relationship("User", foreign_keys=[created_by])
    files: Mapped[list["SharedFolderFile"]] = relationship(
        "SharedFolderFile", back_populates="folder", cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<SharedFolder {self.name} ({self.id[:8]})>"


class SharedFolderFile(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """File record in shared folder with path tracking."""
    __tablename__ = "shared_folder_files"

    folder_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("shared_folders.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    file_record_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("files.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    added_by: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )
    path_in_folder: Mapped[str] = mapped_column(
        String(512), nullable=False,
    )  # e.g., "documents/report.pdf"

    # Relationships
    folder: Mapped["SharedFolder"] = relationship(
        "SharedFolder", back_populates="files", foreign_keys=[folder_id]
    )
    file_record: Mapped["FileRecord"] = relationship("FileRecord", foreign_keys=[file_record_id])
    added_by_user: Mapped["User | None"] = relationship("User", foreign_keys=[added_by])

    def __repr__(self) -> str:
        return f"<SharedFolderFile {self.path_in_folder} ({self.id[:8]})>"
