"""
Voice message model — audio recordings, waveform data, metadata.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class VoiceMessage(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """
    Voice message recording.

    Storage pattern:
    - file_path: absolute path to stored audio file (mp3, wav, etc.)
    - waveform_data: JSON array of peak amplitude samples for UI visualization
    - transcription: optional transcribed text (populated asynchronously)
    """

    __tablename__ = "voice_messages"

    channel_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("channels.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sender_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    duration_ms: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )
    file_path: Mapped[str] = mapped_column(
        String(512),
        nullable=False,
        comment="Absolute path to stored audio file",
    )
    file_size: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        comment="File size in bytes",
    )
    mime_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="audio/mpeg",
        comment="MIME type (audio/mpeg, audio/wav, audio/ogg, etc.)",
    )
    waveform_data: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="JSON array of peak samples for UI visualization",
    )
    transcription: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Optional transcribed text",
    )
    is_read: Mapped[bool] = mapped_column(
        default=False,
        comment="Whether message has been played by at least one recipient",
    )

    # Relationships
    channel: Mapped["Channel"] = relationship("Channel", back_populates="voice_messages")
    sender: Mapped["User"] = relationship("User", back_populates="voice_messages")

    def __repr__(self) -> str:
        return f"<VoiceMessage {self.id[:8]} in {self.channel_id[:8]}>"
