"""
Peer approval service — admin-facing CRUD over the peer state machine.

Owns ALL transitions of `ServerNode.approval_status`. The admin API
routes (`/api/admin/peers/*`) call into this service; the auto-enroll
service also calls into it for verified-but-not-yet-trusted candidates.

Every transition writes a `PeerApprovalAudit` row (when the env flag
enables it) — append-only history for compliance and forensic review.

Security invariants
-------------------
* Approval never happens without a prior successful verification.
  Even an admin can't `approve_peer(server_id)` if the peer's
  `auth_status != "verified"`.
* `cluster_id` mismatch can't be approved over by an admin. The
  cluster_id is checked at verify time; if a row in the table has a
  different cluster_id than the local config, the approval call
  refuses.
* `reject` and `deny` differ in retry behaviour:
    reject → leaves the row, peer can re-discover and re-verify
    deny → adds the fingerprint to the deny cache for the configured
           TTL, so subsequent discoveries short-circuit
* Trust permanently → approves AND clears any deny cache entry.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.session import async_session_factory
from app.models.peer_approval_audit import (
    PeerApprovalAudit,
    AUDIT_ACTION_APPROVED,
    AUDIT_ACTION_REJECTED,
    AUDIT_ACTION_DENIED,
    AUDIT_ACTION_IGNORED,
    AUDIT_ACTION_TRUSTED_PERMA,
    AUDIT_ACTION_TRUSTED_ONCE,
    AUDIT_ACTION_DISCOVERED,
    AUDIT_ACTION_VERIFIED,
    AUDIT_ACTION_AUTH_FAILED,
    AUDIT_ACTION_AUTO_ACCEPTED,
    AUDIT_ACTION_PROVISIONED,
    AUDIT_ACTION_READY,
    AUDIT_ACTION_EVICTED,
)
from app.models.server_node import (
    ACTIVE_PEER_STATES,
    ALL_PEER_STATES,
    PEER_STATE_APPROVED,
    PEER_STATE_AUTH_FAILED,
    PEER_STATE_AUTHENTICATING,
    PEER_STATE_AUTO_ACCEPTED,
    PEER_STATE_AWAITING_HUMAN,
    PEER_STATE_DENIED,
    PEER_STATE_DISCOVERED,
    PEER_STATE_EVICTED,
    PEER_STATE_IGNORED,
    PEER_STATE_PENDING_APPROVAL,
    PEER_STATE_PROVISIONING,
    PEER_STATE_READY,
    PEER_STATE_REJECTED,
    PEER_STATE_REJECTED_BY_ADMIN,
    PEER_STATE_SYNCING_STATE,
    PEER_STATE_VERIFIED,
    PEER_STATE_WAITING_MANUAL_APPROVAL,
    REFUSED_PEER_STATES,
    WAITING_PEER_STATES,
    ServerNode,
)
from app.services.peer_auth import remember_denied, clear_denied

logger = get_logger(__name__)


class PeerApprovalError(Exception):
    """Raised when a transition is not legal."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _audit_enabled() -> bool:
    return bool(get_settings().COMMCLIENT_PEER_APPROVAL_AUDIT_LOG)


class PeerApprovalService:
    """All admin-driven transitions live here. Stateless — operates
    via async DB sessions."""

    # ── Listings ──────────────────────────────────────────────────

    async def list_discovered_peers(self, *, limit: int = 200) -> list[dict]:
        return await self._list_by_status(None, limit=limit)

    async def list_pending_peers(self, *, limit: int = 200) -> list[dict]:
        return await self._list_by_status(WAITING_PEER_STATES, limit=limit)

    async def list_approved_peers(self, *, limit: int = 200) -> list[dict]:
        # Approved + provisioning-in-progress + ready.
        return await self._list_by_status(
            {PEER_STATE_APPROVED, PEER_STATE_PROVISIONING,
             PEER_STATE_SYNCING_STATE, PEER_STATE_READY,
             PEER_STATE_AUTO_ACCEPTED},
            limit=limit,
        )

    async def list_rejected_peers(self, *, limit: int = 200) -> list[dict]:
        return await self._list_by_status(
            {PEER_STATE_REJECTED, PEER_STATE_REJECTED_BY_ADMIN,
             PEER_STATE_AUTH_FAILED, PEER_STATE_IGNORED},
            limit=limit,
        )

    async def list_denied_peers(self, *, limit: int = 200) -> list[dict]:
        return await self._list_by_status({PEER_STATE_DENIED}, limit=limit)

    async def _list_by_status(
        self, statuses: Optional[set[str]], *, limit: int,
    ) -> list[dict]:
        async with async_session_factory() as db:
            q = select(ServerNode).order_by(ServerNode.last_seen_at.desc().nullslast())
            if statuses:
                q = q.where(ServerNode.approval_status.in_(statuses))
            q = q.limit(limit)
            rows = (await db.execute(q)).scalars().all()
            return [self._row_to_dict(r) for r in rows]

    # ── Transitions (admin-driven) ────────────────────────────────

    async def approve_peer(
        self, server_id: str, admin_user_id: str,
    ) -> dict:
        """Move from WAITING_*/AWAITING_HUMAN/AUTO_ACCEPTED → APPROVED.
        Refuses if peer auth_status != "verified" or cluster_id
        doesn't match local."""
        return await self._admin_transition(
            server_id=server_id,
            admin_user_id=admin_user_id,
            new_status=PEER_STATE_APPROVED,
            audit_action=AUDIT_ACTION_APPROVED,
            require_verified=True,
            require_cluster_match=True,
            allowed_from=WAITING_PEER_STATES | {PEER_STATE_AUTO_ACCEPTED},
        )

    async def reject_peer(
        self, server_id: str, admin_user_id: str, reason: str,
    ) -> dict:
        """Reject — peer can re-discover and re-verify later. NOT the
        same as deny."""
        if not reason:
            raise PeerApprovalError("reject_reason_required")
        return await self._admin_transition(
            server_id=server_id,
            admin_user_id=admin_user_id,
            new_status=PEER_STATE_REJECTED_BY_ADMIN,
            audit_action=AUDIT_ACTION_REJECTED,
            require_verified=False,
            require_cluster_match=False,
            allowed_from=WAITING_PEER_STATES | {PEER_STATE_AUTO_ACCEPTED},
            reason=reason,
        )

    async def deny_peer(
        self, server_id: str, admin_user_id: str, reason: str,
    ) -> dict:
        """Deny — adds fingerprint to deny cache so re-discovery is
        short-circuited. Used for known-bad peers."""
        if not reason:
            raise PeerApprovalError("deny_reason_required")
        result = await self._admin_transition(
            server_id=server_id,
            admin_user_id=admin_user_id,
            new_status=PEER_STATE_DENIED,
            audit_action=AUDIT_ACTION_DENIED,
            require_verified=False,
            require_cluster_match=False,
            allowed_from=ALL_PEER_STATES,
            reason=reason,
        )
        # Push fingerprint into deny cache.
        fp = result.get("public_key_fingerprint")
        if fp:
            await remember_denied(fp, reason)
        return result

    async def ignore_peer(
        self, server_id: str, admin_user_id: str,
    ) -> dict:
        """Ignore — peer stays in DB but never shows up in admin UI
        listings. Less aggressive than deny."""
        return await self._admin_transition(
            server_id=server_id,
            admin_user_id=admin_user_id,
            new_status=PEER_STATE_IGNORED,
            audit_action=AUDIT_ACTION_IGNORED,
            require_verified=False,
            require_cluster_match=False,
            allowed_from=ALL_PEER_STATES,
        )

    async def trust_peer_permanently(
        self, server_id: str, admin_user_id: str,
    ) -> dict:
        """Approve AND clear any deny entry. The peer is treated as
        trusted on every subsequent re-discovery (auto-approves into
        APPROVED without further admin action) until explicitly
        revoked."""
        result = await self._admin_transition(
            server_id=server_id,
            admin_user_id=admin_user_id,
            new_status=PEER_STATE_APPROVED,
            audit_action=AUDIT_ACTION_TRUSTED_PERMA,
            require_verified=True,
            require_cluster_match=True,
            allowed_from=WAITING_PEER_STATES | {PEER_STATE_AUTO_ACCEPTED},
            metadata={"trust": "permanent"},
        )
        fp = result.get("public_key_fingerprint")
        if fp:
            await clear_denied(fp)
        return result

    async def trust_peer_once(
        self, server_id: str, admin_user_id: str,
    ) -> dict:
        """Approve THIS time only. The peer goes back to manual
        approval on the next re-discovery cycle. Implementation: same
        as approve, but the audit action carries the trust=once tag
        so the next discovery handler can distinguish."""
        return await self._admin_transition(
            server_id=server_id,
            admin_user_id=admin_user_id,
            new_status=PEER_STATE_APPROVED,
            audit_action=AUDIT_ACTION_TRUSTED_ONCE,
            require_verified=True,
            require_cluster_match=True,
            allowed_from=WAITING_PEER_STATES | {PEER_STATE_AUTO_ACCEPTED},
            metadata={"trust": "once"},
        )

    # ── Internal helpers ─────────────────────────────────────────

    async def _admin_transition(
        self,
        *,
        server_id: str,
        admin_user_id: str,
        new_status: str,
        audit_action: str,
        require_verified: bool,
        require_cluster_match: bool,
        allowed_from: set[str],
        reason: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict:
        async with async_session_factory() as db:
            row = (await db.execute(
                select(ServerNode).where(ServerNode.server_id == server_id)
            )).scalar_one_or_none()
            if row is None:
                raise PeerApprovalError(f"unknown_peer:{server_id}")

            old_status = row.approval_status
            if old_status not in allowed_from:
                raise PeerApprovalError(
                    f"illegal_transition_from:{old_status}_to:{new_status}"
                )

            if require_verified and row.auth_status != "verified":
                raise PeerApprovalError(
                    f"not_verified:auth_status={row.auth_status}"
                )

            if require_cluster_match:
                local_cluster = (get_settings().COMMCLIENT_CLUSTER_ID or "").strip()
                if row.cluster_id != local_cluster:
                    raise PeerApprovalError(
                        f"cluster_mismatch:peer={row.cluster_id} "
                        f"local={local_cluster}"
                    )

            now = _utc_now()
            row.approval_status = new_status

            if new_status == PEER_STATE_APPROVED:
                row.approved_at = now
                row.approved_by = admin_user_id
            elif new_status == PEER_STATE_REJECTED_BY_ADMIN:
                row.rejected_at = now
                row.rejected_by = admin_user_id
                row.reject_reason = reason
            elif new_status == PEER_STATE_DENIED:
                row.denied_at = now
                row.denied_by = admin_user_id
                row.deny_reason = reason

            db.add(row)
            await self._write_audit(
                db, row, audit_action, admin_user_id, reason,
                old_status, new_status, metadata,
            )
            await db.commit()
            await db.refresh(row)
            result = self._row_to_dict(row)

        logger.info(
            "peer_admin_transition",
            server_id=server_id, action=audit_action,
            old_status=old_status, new_status=new_status,
            admin_user_id=admin_user_id, reason=reason,
        )
        return result

    async def record_lifecycle_transition(
        self,
        *,
        server_id: str,
        old_status: str,
        new_status: str,
        action: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """System-driven transitions (auto_accept, auth_failed, ready,
        evicted). Goes into the audit table but doesn't apply admin-
        only checks."""
        async with async_session_factory() as db:
            row = (await db.execute(
                select(ServerNode).where(ServerNode.server_id == server_id)
            )).scalar_one_or_none()
            if row is None:
                logger.warning("audit_record_unknown_peer", server_id=server_id)
                return
            await self._write_audit(
                db, row, action, None, None,
                old_status, new_status, metadata,
            )
            await db.commit()

    async def _write_audit(
        self,
        db: AsyncSession,
        row: ServerNode,
        action: str,
        admin_user_id: str | None,
        reason: str | None,
        old_status: str | None,
        new_status: str | None,
        metadata: dict[str, Any] | None,
    ) -> None:
        if not _audit_enabled():
            return
        try:
            db.add(PeerApprovalAudit(
                server_node_id=row.id,
                server_id=row.server_id,
                action=action,
                admin_user_id=admin_user_id,
                reason=reason,
                old_status=old_status,
                new_status=new_status,
                metadata_json=json.dumps(metadata) if metadata else None,
            ))
        except Exception as e:
            logger.warning("audit_write_failed", error=str(e))

    # ── Auto-eviction sweeper ────────────────────────────────────

    async def evict_stale_waiting(self) -> int:
        """Walk WAITING_*/PENDING/AWAITING rows and evict ones whose
        last_seen_at exceeds COMMCLIENT_PEER_PENDING_TTL_SECONDS.

        Returns the number of rows evicted. Best-effort — failures
        are logged but never raise.
        """
        from datetime import timedelta
        from app.core.config import get_settings as _gs
        ttl = int(_gs().COMMCLIENT_PEER_PENDING_TTL_SECONDS or 86_400)
        cutoff = _utc_now() - timedelta(seconds=ttl)
        evicted = 0
        try:
            async with async_session_factory() as db:
                rows = (await db.execute(
                    select(ServerNode).where(
                        ServerNode.approval_status.in_(WAITING_PEER_STATES),
                        ServerNode.last_seen_at < cutoff,
                    )
                )).scalars().all()
                for row in rows:
                    old = row.approval_status
                    row.approval_status = PEER_STATE_EVICTED
                    db.add(row)
                    await self._write_audit(
                        db, row,
                        action="evicted",  # AUDIT_ACTION_EVICTED constant
                        admin_user_id=None,
                        reason="stale_waiting_ttl_exceeded",
                        old_status=old, new_status=PEER_STATE_EVICTED,
                        metadata={"ttl_seconds": ttl},
                    )
                    evicted += 1
                if evicted:
                    await db.commit()
                    logger.info(
                        "peer_eviction_sweeper",
                        count=evicted, ttl_seconds=ttl,
                    )
        except Exception as e:
            logger.warning("peer_eviction_sweeper_failed", error=str(e))
        return evicted

    # ── Fast lookups for hot paths ───────────────────────────────

    async def is_peer_routable(self, server_id: str) -> bool:
        """Fast read used by route_executor + fabric_subscribers
        before accepting a forward FROM ``server_id``. Only peers in
        ACTIVE_PEER_STATES (READY / DEGRADED) may originate fabric
        traffic — anything else (DISCOVERED, WAITING, PENDING,
        REJECTED, DENIED, EVICTED) is silently ignored.

        Returns False on lookup failure too — fail-closed.

        Note: returns False for both "known-but-inactive" and
        "unknown peer". Callers that need to distinguish (e.g. the
        federation HTTP gate which wants legacy fail-open for
        first-contact) should use ``get_peer_status`` instead."""
        return (await self.get_peer_status(server_id)) in ACTIVE_PEER_STATES

    async def get_peer_status(self, server_id: str) -> str | None:
        """Return the raw approval_status for ``server_id``, or None
        if the peer has never been enrolled. Used by the federation
        HTTP gate to distinguish "explicitly rejected/denied" from
        "we've never heard of this peer". Fail-closed by returning
        an empty string sentinel on lookup error."""
        try:
            async with async_session_factory() as db:
                row = (await db.execute(
                    select(ServerNode.approval_status).where(
                        ServerNode.server_id == server_id
                    )
                )).scalar_one_or_none()
            return row  # None if no row
        except Exception as e:
            logger.warning("get_peer_status_failed",
                           server_id=server_id, error=str(e))
            return ""  # treat lookup error as "denied"

    @staticmethod
    def _row_to_dict(row: ServerNode) -> dict:
        return {
            "id": row.id,
            "server_id": row.server_id,
            "cluster_id": row.cluster_id,
            "region": row.region,
            "zone": row.zone,
            "endpoint": row.endpoint,
            "version": row.version,
            "capabilities": row.capabilities,
            "public_key_fingerprint": row.public_key_fingerprint,
            "discovery_method": row.discovery_method,
            "auth_status": row.auth_status,
            "acceptance_mode": row.acceptance_mode,
            "approval_status": row.approval_status,
            "runtime_status": row.runtime_status,
            "last_seen_at": row.last_seen_at.isoformat() if row.last_seen_at else None,
            "approved_at": row.approved_at.isoformat() if row.approved_at else None,
            "approved_by": row.approved_by,
            "rejected_at": row.rejected_at.isoformat() if row.rejected_at else None,
            "rejected_by": row.rejected_by,
            "reject_reason": row.reject_reason,
            "denied_at": row.denied_at.isoformat() if row.denied_at else None,
            "denied_by": row.denied_by,
            "deny_reason": row.deny_reason,
            "boot_id": row.boot_id,
            "fencing_token": row.fencing_token,
        }


# ── Module-level singleton ──────────────────────────────────────────

peer_approval_service = PeerApprovalService()
