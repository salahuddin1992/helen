"""
ServerNode — durable record of every peer Helen-Server discovered on
the cluster.

Why a dedicated table
---------------------
``app.services.peer_registry`` keeps a hot in-memory index of live
peers. That's enough for routing decisions but loses everything on
restart, can't audit "who approved peer X two months ago", and
doesn't survive multi-worker setups where each worker has its own
view.

This table is the durable mirror. Every state transition (DISCOVERED
→ AUTHENTICATING → … → READY) is persisted. The PeerApprovalService
reads/writes here; the in-memory peer_registry stays as a fast cache.

Lifecycle
---------
::

    DISCOVERED → AUTHENTICATING → VERIFIED → AUTO_ACCEPTED (auto_accept mode)
                                          → WAITING_MANUAL_APPROVAL (manual_approval mode)
                                          → PENDING_APPROVAL (pending_approval mode)
                                          → AWAITING_HUMAN_SELECTION (human_selection mode)
                                          → APPROVED → PROVISIONING → SYNCING_STATE → READY
                                          → REJECTED / REJECTED_BY_ADMIN / DENIED / IGNORED

Failure transitions:
::

    AUTHENTICATING → AUTH_FAILED → REJECTED
    READY → DEGRADED → UNHEALTHY → EVICTED
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, utc_now


# Acceptance state — kept as plain strings for cross-process readability.
# Code uses constants (see peer_acceptance_policy.PeerState) but the
# DB type is string so old/new servers sharing the table can interpret
# unknown values as a forward-compat hint.
PEER_STATE_DISCOVERED              = "DISCOVERED"
PEER_STATE_AUTHENTICATING          = "AUTHENTICATING"
PEER_STATE_AUTH_FAILED             = "AUTH_FAILED"
PEER_STATE_VERIFIED                = "VERIFIED"
PEER_STATE_AUTO_ACCEPTED           = "AUTO_ACCEPTED"
PEER_STATE_WAITING_MANUAL_APPROVAL = "WAITING_MANUAL_APPROVAL"
PEER_STATE_PENDING_APPROVAL        = "PENDING_APPROVAL"
PEER_STATE_AWAITING_HUMAN          = "AWAITING_HUMAN_SELECTION"
PEER_STATE_APPROVED                = "APPROVED"
PEER_STATE_REJECTED                = "REJECTED"
PEER_STATE_REJECTED_BY_ADMIN       = "REJECTED_BY_ADMIN"
PEER_STATE_DENIED                  = "DENIED"
PEER_STATE_IGNORED                 = "IGNORED"
PEER_STATE_PROVISIONING            = "PROVISIONING"
PEER_STATE_SYNCING_STATE           = "SYNCING_STATE"
PEER_STATE_READY                   = "READY"
PEER_STATE_DEGRADED                = "DEGRADED"
PEER_STATE_UNHEALTHY               = "UNHEALTHY"
PEER_STATE_EVICTED                 = "EVICTED"

ALL_PEER_STATES = frozenset({
    PEER_STATE_DISCOVERED, PEER_STATE_AUTHENTICATING, PEER_STATE_AUTH_FAILED,
    PEER_STATE_VERIFIED, PEER_STATE_AUTO_ACCEPTED,
    PEER_STATE_WAITING_MANUAL_APPROVAL, PEER_STATE_PENDING_APPROVAL,
    PEER_STATE_AWAITING_HUMAN, PEER_STATE_APPROVED, PEER_STATE_REJECTED,
    PEER_STATE_REJECTED_BY_ADMIN, PEER_STATE_DENIED, PEER_STATE_IGNORED,
    PEER_STATE_PROVISIONING, PEER_STATE_SYNCING_STATE, PEER_STATE_READY,
    PEER_STATE_DEGRADED, PEER_STATE_UNHEALTHY, PEER_STATE_EVICTED,
})

# Convenience grouping. "Active" = peer is currently routable.
ACTIVE_PEER_STATES = frozenset({PEER_STATE_READY, PEER_STATE_DEGRADED})
# "Waiting on admin" — admin UI shows these.
WAITING_PEER_STATES = frozenset({
    PEER_STATE_WAITING_MANUAL_APPROVAL,
    PEER_STATE_PENDING_APPROVAL,
    PEER_STATE_AWAITING_HUMAN,
})
# "Refused" — kept for audit + deny-cache lookup.
REFUSED_PEER_STATES = frozenset({
    PEER_STATE_REJECTED, PEER_STATE_REJECTED_BY_ADMIN,
    PEER_STATE_DENIED, PEER_STATE_IGNORED,
    PEER_STATE_AUTH_FAILED, PEER_STATE_EVICTED,
})
# "Transient" — peer is mid-enrollment. The federation HTTP gate fails
# OPEN on these (HMAC alone gates) because blocking them would create
# a chicken-and-egg with peer announcements: the second peer tries to
# push presence right after enrolling, but its own enrollment row on
# this side is still racing through the state machine. HMAC + cluster
# match is enough security here — these peers will either reach READY
# (and the next request short-circuits at "active") or land in WAITING/
# REFUSED (where the gate then refuses).
TRANSIENT_PEER_STATES = frozenset({
    PEER_STATE_DISCOVERED,
    PEER_STATE_AUTHENTICATING,
    PEER_STATE_VERIFIED,
    PEER_STATE_AUTO_ACCEPTED,
    PEER_STATE_APPROVED,
    PEER_STATE_PROVISIONING,
    PEER_STATE_SYNCING_STATE,
})


class ServerNode(Base, TimestampMixin):
    """One row per peer. server_id is the unique key (we trust the
    peer to use the same identity across reconnects)."""

    __tablename__ = "server_nodes"

    # We keep an internal id PK so reassigning a server_id on takeover
    # doesn't break referential integrity. server_id is unique-per-row
    # via UniqueConstraint below.
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    server_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    cluster_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    region: Mapped[str | None] = mapped_column(String(32), nullable=True)
    zone:   Mapped[str | None] = mapped_column(String(32), nullable=True)

    # Reachable URL (e.g. https://10.1.2.3:3000). None means we only
    # know the peer abstractly and routing is impossible until we
    # learn an endpoint.
    endpoint: Mapped[str | None] = mapped_column(String(512), nullable=True)
    version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Comma-separated capabilities the peer announces ("sfu,turn,…").
    capabilities: Mapped[str | None] = mapped_column(String(256), nullable=True)

    # SHA-256 fingerprint of the peer's public key / HMAC keying material.
    # Doubles as the deny-cache key — a peer changing keys produces a
    # different fingerprint and re-enters the approval flow.
    public_key_fingerprint: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True,
    )

    # How we discovered the peer. udp_broadcast / mdns / dht / manual /
    # rendezvous. Audit-only — the approval logic doesn't branch on it.
    discovery_method: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # Auth verification result.
    # auth_status ∈ {"unknown", "verified", "failed"}.
    auth_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="unknown",
    )

    # Which mode the LOCAL server was running when this row was last
    # touched. Useful for audit ("why is this peer in WAITING when
    # the cluster is in auto_accept now?" → mode changed mid-flight).
    acceptance_mode: Mapped[str] = mapped_column(
        String(32), nullable=False, default="manual_approval",
    )

    # Approval state (one of PEER_STATE_*).
    approval_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=PEER_STATE_DISCOVERED, index=True,
    )

    # Runtime status — DEGRADED / UNHEALTHY / EVICTED set by the load
    # monitor + heartbeat sweeper. Distinct from approval_status; a
    # peer can be APPROVED but DEGRADED (route around it).
    runtime_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="unknown",
    )

    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True,
    )

    approved_at:  Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_by:  Mapped[str | None]      = mapped_column(String(32), nullable=True)
    rejected_at:  Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejected_by:  Mapped[str | None]      = mapped_column(String(32), nullable=True)
    reject_reason: Mapped[str | None]     = mapped_column(String(512), nullable=True)
    denied_at:    Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    denied_by:    Mapped[str | None]      = mapped_column(String(32), nullable=True)
    deny_reason:  Mapped[str | None]      = mapped_column(String(512), nullable=True)

    # Boot ID + fencing token — when a server reboots without our
    # knowing, boot_id changes. fencing_token is a monotonic counter
    # bumped on each takeover so a stale message from before the
    # reboot can be detected and dropped.
    boot_id: Mapped[str | None]        = mapped_column(String(64), nullable=True)
    fencing_token: Mapped[int]         = mapped_column(Integer, nullable=False, default=0)

    # Optional metadata — JSON-encoded extra fields (e.g. region tag,
    # admin notes) for forward compatibility without schema churn.
    metadata_json: Mapped[str | None]  = mapped_column(Text, nullable=True)

    audits: Mapped[list["PeerApprovalAudit"]] = relationship(
        "PeerApprovalAudit",
        back_populates="server",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        UniqueConstraint("server_id", name="uq_server_nodes_server_id"),
        Index("ix_server_nodes_cluster_status",
              "cluster_id", "approval_status"),
    )

    def __repr__(self) -> str:
        return (
            f"<ServerNode id={self.id} server_id={self.server_id[:16]} "
            f"status={self.approval_status} cluster={self.cluster_id}>"
        )
