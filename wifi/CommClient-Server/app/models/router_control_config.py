"""
RouterControlConfig — single-row override table for the Helen-Router
admin proxy.

The proxy in ``app/services/router_control`` falls back to env vars
when this row is missing, so a deployment that doesn't run
migrations keeps working — the table is purely a *runtime
override* the admin UI can mutate without redeploying.

There is exactly ONE row, with ``id=1``. The schema is kept
intentionally narrow; rotating tokens or changing the URL is
captured in the audit log, not by spawning new rows.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, utc_now


class RouterControlConfig(Base):
    __tablename__ = "router_control_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    base_url: Mapped[str] = mapped_column(
        String(512), nullable=False, default="",
    )
    token: Mapped[str] = mapped_column(
        Text, nullable=False, default="",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now, onupdate=utc_now,
        nullable=False,
    )

    def __repr__(self) -> str:                    # pragma: no cover
        return (f"<RouterControlConfig id={self.id} "
                f"base_url={self.base_url!r} token_set={bool(self.token)}>")
