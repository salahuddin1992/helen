"""
Phase 7 / Module AH — Long-running plugin job state.

A row per install / upgrade / uninstall operation. The installer
service writes phase + progress here, and the WebSocket fan-out
mirrors the same events to subscribers. Jobs survive process restarts.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, utc_now


VALID_JOB_KINDS = (
    "install", "upgrade", "uninstall", "sandbox_preview", "upload",
)
VALID_JOB_STATES = (
    "pending", "running", "succeeded", "failed", "rolled_back", "cancelled",
)


class PluginJob(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "plugin_jobs"
    __table_args__ = (
        Index("ix_plugin_jobs_slug", "slug"),
        Index("ix_plugin_jobs_state", "state"),
        Index("ix_plugin_jobs_started_at", "started_at"),
    )

    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending",
        server_default="pending",
    )
    phase: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    pct: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    actor_id: Mapped[Optional[str]] = mapped_column(
        String(32),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    detail: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default="{}",
    )
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    def mark_running(self, phase: str = "init") -> None:
        self.state = "running"
        self.phase = phase
        if self.started_at is None:
            self.started_at = utc_now()

    def mark_succeeded(self) -> None:
        self.state = "succeeded"
        self.pct = 100
        self.finished_at = utc_now()

    def mark_failed(self, error: str) -> None:
        self.state = "failed"
        self.error_message = error[:8192]
        self.finished_at = utc_now()
