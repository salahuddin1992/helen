"""
Phase 7 / Module AJ — Federation v2 Mesh models.

Five tables implementing a Matrix/XMPP-style federation:

    federation_v2_servers       — known peer servers
    federation_v2_users         — federated identities (user@server)
    federation_v2_channels      — shared channels across the mesh
    federation_v2_events        — signed event DAG
    federation_v2_trust_tokens  — cross-server trust tokens

All tables follow the established UUIDPrimaryKey + Timestamp pattern.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, utc_now


VALID_FEDV2_SERVER_STATUSES = ("active", "suspended", "banned", "pending")
VALID_FEDV2_TRUST_LEVELS = ("trusted", "peer", "restricted", "untrusted")
VALID_FEDV2_EVENT_KINDS = (
    "message", "edit", "delete",
    "membership", "presence", "typing",
    "reaction", "state", "redaction",
)
VALID_FEDV2_CHANNEL_POLICIES = ("public", "invite_only", "restricted")
VALID_FEDV2_SIGNING_ALGOS = ("ed25519", "ed25519-v2")


class FederatedServer(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Remote Helen server that we federate with.

    ``server_id`` is the DNS-style identity (e.g. ``helen.example.org``)
    and is the unique federation address. ``public_key`` is the
    server's Ed25519 verification key (base64 raw, 32 bytes).
    """

    __tablename__ = "federation_v2_servers"
    __table_args__ = (
        UniqueConstraint("server_id", name="uq_fedv2_servers_server_id"),
        Index("ix_fedv2_servers_status", "status"),
        Index("ix_fedv2_servers_trust", "trust_level"),
        Index("ix_fedv2_servers_last_seen", "last_seen"),
    )

    server_id: Mapped[str] = mapped_column(String(255), nullable=False)
    public_key: Mapped[str] = mapped_column(String(512), nullable=False)
    advertise_url: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending",
    )
    trust_level: Mapped[str] = mapped_column(
        String(16), nullable=False, default="peer", server_default="peer",
    )
    trust_score: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.5, server_default="0.5",
    )
    version: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    signing_algo: Mapped[str] = mapped_column(
        String(32), nullable=False, default="ed25519", server_default="ed25519",
    )
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now,
    )
    suspended_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<FederatedServer {self.server_id} {self.status}/{self.trust_level}>"


class FederatedUser(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Federated user identity (remote or aliased local). Address form:
    ``user@server.example``. ``local_user_id`` is non-null if this is a
    cached pointer to a local user (for outbound presence broadcast)."""

    __tablename__ = "federation_v2_users"
    __table_args__ = (
        UniqueConstraint("remote_address", name="uq_fedv2_users_addr"),
        Index("ix_fedv2_users_local_user", "local_user_id"),
        Index("ix_fedv2_users_server", "origin_server"),
        Index("ix_fedv2_users_last_seen", "last_seen"),
    )

    local_user_id: Mapped[Optional[str]] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )
    remote_address: Mapped[str] = mapped_column(String(320), nullable=False)
    origin_server: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    avatar_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    public_key: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now,
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<FederatedUser {self.remote_address}>"


class FederatedChannel(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Cross-server shared channel descriptor."""

    __tablename__ = "federation_v2_channels"
    __table_args__ = (
        UniqueConstraint("federation_address", name="uq_fedv2_channels_addr"),
        Index("ix_fedv2_channels_channel", "channel_id"),
        Index("ix_fedv2_channels_policy", "policy"),
    )

    channel_id: Mapped[str] = mapped_column(String(32), nullable=False)
    federation_address: Mapped[str] = mapped_column(String(320), nullable=False)
    origin_server: Mapped[str] = mapped_column(String(255), nullable=False)
    shared_with: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    policy: Mapped[str] = mapped_column(
        String(16), nullable=False, default="public", server_default="public",
    )
    state_version: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0",
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<FederatedChannel {self.federation_address} {self.policy}>"


class FederationEvent(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Single event in the signed DAG.

    Events form a Merkle-style DAG: each event references prior events
    via ``dag_parents`` and is signed by its origin server. Conflict
    resolution is deterministic — see ``services.federation_v2.dag``.
    """

    __tablename__ = "federation_v2_events"
    __table_args__ = (
        UniqueConstraint("origin_server", "origin_event_id",
                         name="uq_fedv2_events_origin"),
        Index("ix_fedv2_events_kind", "kind"),
        Index("ix_fedv2_events_channel", "channel_address"),
        Index("ix_fedv2_events_processed", "processed"),
        Index("ix_fedv2_events_depth", "depth"),
        Index("ix_fedv2_events_origin_server", "origin_server"),
    )

    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    origin_server: Mapped[str] = mapped_column(String(255), nullable=False)
    origin_event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    channel_address: Mapped[Optional[str]] = mapped_column(String(320), nullable=True)
    sender_address: Mapped[Optional[str]] = mapped_column(String(320), nullable=True)
    signed_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    dag_parents: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    depth: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    processed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0",
    )
    processed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    rejected: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0",
    )
    rejection_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<FederationEvent {self.origin_event_id[:12]} {self.kind}>"


class FederationTrustToken(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Cross-server trust token. Allows web-of-trust attestations and
    delegated capabilities (e.g. server A vouches for server B)."""

    __tablename__ = "federation_v2_trust_tokens"
    __table_args__ = (
        Index("ix_fedv2_tokens_issuer", "issuing_server"),
        Index("ix_fedv2_tokens_subject", "subject_server"),
        Index("ix_fedv2_tokens_expires", "expires_at"),
    )

    issuing_server: Mapped[str] = mapped_column(String(255), nullable=False)
    subject_server: Mapped[str] = mapped_column(String(255), nullable=False)
    signed_token: Mapped[str] = mapped_column(Text, nullable=False)
    scope: Mapped[str] = mapped_column(String(64), nullable=False, default="peer")
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now,
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    revoked: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0",
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<FederationTrustToken {self.issuing_server}->{self.subject_server}>"
