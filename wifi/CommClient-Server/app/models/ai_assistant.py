"""
Phase 5 / Module Z — AI Assistant models.

Four tables:
    ai_configs   — per-workspace provider config (one active per workspace)
    ai_sessions  — a logical conversation (chat / summary / search / draft)
    ai_messages  — every prompt / response with metrics and PII redaction map
    ai_opt_ins   — explicit per-user opt-in within a workspace
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
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


VALID_AI_PROVIDERS = ("anthropic", "openai", "ollama", "none")
VALID_AI_KINDS = ("summary", "search", "draft", "chat")
VALID_AI_SCOPES = ("all", "summarize", "search", "draft")
VALID_AI_ROLES = ("system", "user", "assistant", "tool")


class AIConfig(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "ai_configs"
    __table_args__ = (
        UniqueConstraint("workspace_id", name="uq_ai_config_workspace"),
        Index("ix_ai_configs_workspace_id", "workspace_id"),
    )

    workspace_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(
        String(32), nullable=False, default="none", server_default="none",
    )
    model_name: Mapped[str] = mapped_column(
        String(128), nullable=False, default="claude-3-5-sonnet-latest",
    )
    api_key_secret_ref: Mapped[str | None] = mapped_column(
        String(128), nullable=True,
        comment="key in app.services.secret_store (do NOT store API key here)",
    )
    base_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0",
    )
    settings: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict,
    )

    sessions: Mapped[list["AISession"]] = relationship(
        "AISession", back_populates="config",
        cascade="all, delete-orphan", lazy="noload",
    )


class AISession(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "ai_sessions"
    __table_args__ = (
        Index("ix_ai_sessions_user_id", "user_id"),
        Index("ix_ai_sessions_workspace_id", "workspace_id"),
        Index("ix_ai_sessions_kind", "kind"),
        Index("ix_ai_sessions_created_at", "created_at"),
    )

    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
    )
    workspace_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    config_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("ai_configs.id", ondelete="SET NULL"),
        nullable=True,
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now,
    )

    config: Mapped["AIConfig | None"] = relationship(
        "AIConfig", back_populates="sessions",
    )
    messages: Mapped[list["AIMessage"]] = relationship(
        "AIMessage", back_populates="session",
        cascade="all, delete-orphan", lazy="noload",
    )


class AIMessage(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "ai_messages"
    __table_args__ = (
        Index("ix_ai_messages_session_id", "session_id"),
        Index("ix_ai_messages_created_at", "created_at"),
    )

    session_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("ai_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    tokens_used: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    latency_ms: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    redacted_pii: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict,
    )
    cost_micro_usd: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now,
    )

    session: Mapped[AISession] = relationship(
        "AISession", back_populates="messages",
    )


class AIOptIn(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "ai_opt_ins"
    __table_args__ = (
        UniqueConstraint("workspace_id", "user_id", name="uq_ai_optin"),
        Index("ix_ai_opt_ins_user_id", "user_id"),
    )

    workspace_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
    )
    scope: Mapped[str] = mapped_column(
        String(16), nullable=False, default="all", server_default="all",
    )
    opted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now,
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
