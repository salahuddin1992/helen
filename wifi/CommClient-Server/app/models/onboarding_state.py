"""
Onboarding state model — single-row record tracking the operator onboarding
wizard's progress across the 14 declarative steps.

Row PK is hard-coded to ``1`` so the table behaves as a singleton — this is
the canonical pattern for system-wide configuration that must always exist.

Fields
------
completed_steps     JSON list of int step numbers fully validated + applied.
current_step        Cursor pointing at the next step the operator should run.
draft_data          JSON dict keyed by step number containing partial input
                    that has not yet been finalized via ``/complete``.
started_at          When the operator first opened the wizard.
finalized_at        When ``/complete`` succeeded.
locked              True once the wizard is finalized; admin-only reset clears.
actor_id            Last actor (user_id) who touched the wizard.
metadata            Free-form JSON for client UI state (theme, language, …).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, utc_now


class OnboardingState(Base):
    __tablename__ = "onboarding_state"

    # Single-row singleton — id is always 1.
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)

    completed_steps: Mapped[list[int]] = mapped_column(
        JSON, nullable=False, default=list,
    )
    current_step: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1,
    )
    draft_data: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict,
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now,
    )
    finalized_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    locked: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False,
    )
    actor_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
    )
    extra_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=utc_now, onupdate=utc_now,
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "completed_steps": list(self.completed_steps or []),
            "current_step": self.current_step,
            "draft_data": dict(self.draft_data or {}),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finalized_at": self.finalized_at.isoformat() if self.finalized_at else None,
            "locked": bool(self.locked),
            "actor_id": self.actor_id,
            "extra_metadata": dict(self.extra_metadata or {}),
        }
