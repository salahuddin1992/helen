"""
PeerApprovalAudit — append-only log of every peer-state change made
by an admin (or by the auto_accept policy on the admin's behalf).

Why
---
The ServerNode table tracks the CURRENT approval_status. This table
keeps the HISTORY: every approve/reject/deny/ignore/trust action
with the actor, reason, and the state transition itself. Required
for compliance audits and post-incident review ("who approved peer
X right before the breach?").

Append-only: there is no UPDATE pathway. Every action emits one row.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, utc_now


# Action verbs mirrored on the admin API surface.
AUDIT_ACTION_DISCOVERED      = "discovered"
AUDIT_ACTION_VERIFIED        = "verified"
AUDIT_ACTION_AUTH_FAILED     = "auth_failed"
AUDIT_ACTION_AUTO_ACCEPTED   = "auto_accepted"
AUDIT_ACTION_APPROVED        = "approved"
AUDIT_ACTION_REJECTED        = "rejected"
AUDIT_ACTION_DENIED          = "denied"
AUDIT_ACTION_IGNORED         = "ignored"
AUDIT_ACTION_TRUSTED_PERMA   = "trusted_permanently"
AUDIT_ACTION_TRUSTED_ONCE    = "trusted_once"
AUDIT_ACTION_PROVISIONED     = "provisioned"
AUDIT_ACTION_READY           = "ready"
AUDIT_ACTION_EVICTED         = "evicted"


class PeerApprovalAudit(Base, TimestampMixin):
    __tablename__ = "peer_approval_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # FK to server_nodes.id; CASCADE so removing a peer cleans the trail.
    # If we ever want to keep the audit even after peer eviction, swap
    # to ondelete='SET NULL'.
    server_node_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("server_nodes.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    # Denormalized for ad-hoc queries even after server_nodes row is
    # gone. Required when we ever switch the FK to SET NULL.
    server_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    action: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    # The admin user that took the action. NULL for system actions
    # (auto_accept, auto-eviction, etc.).
    admin_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # Free-form reason text. Required by the API for reject/deny so an
    # operator reviewing the audit log knows why.
    reason: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # State transition recorded at the moment of the action.
    old_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    new_status: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # Optional opaque JSON for forensic context (the verify result
    # snapshot, the admin's IP, the discovery method, etc.).
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    server: Mapped["ServerNode"] = relationship(  # type: ignore[name-defined]
        "ServerNode", back_populates="audits",
    )

    __table_args__ = (
        Index("ix_peer_audit_server_action", "server_id", "action"),
        Index("ix_peer_audit_created", "created_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<PeerApprovalAudit id={self.id} server={self.server_id[:16]} "
            f"action={self.action}>"
        )
