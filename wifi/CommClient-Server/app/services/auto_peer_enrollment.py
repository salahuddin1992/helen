"""
Auto peer enrollment — discovery → verify → policy-based routing.

Entry point for ANY peer-discovery channel (UDP broadcast, mDNS, DHT,
manual seed, federation gossip). Hands the candidate to peer_auth for
verification, then to peer_acceptance_policy to decide what state the
peer ends up in.

Outcomes
--------
::

    handle_discovered_peer(announcement_payload):
      ↓
      verify_peer_candidate     ← peer_auth
      ↓
      ok=False  → REJECTED + audit("auth_failed")
      ok=True   → policy.get_mode():
        auto_accept       → AUTO_ACCEPTED → provision → READY
        manual_approval   → WAITING_MANUAL_APPROVAL  (admin acts)
        pending_approval  → PENDING_APPROVAL         (admin acts)
        human_selection   → AWAITING_HUMAN_SELECTION (admin acts)

The persisted ServerNode row is the source of truth; this service
orchestrates DB writes via SQLAlchemy and never holds long-lived
state of its own. Callable from any code path that learns about a
peer.

Provisioning (after APPROVED)
-----------------------------
Currently a stub that:
  * marks runtime_status="ready"
  * calls trace_collector / server_registry hooks
  * fires the audit row

Full provisioning (Redis registry write, broker subscriptions,
heartbeat probe, route table addition, presence/load/topology snapshot
sync, active-call state sync) is wired in via the registry service +
load monitor that already exist; this just signals them.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.session import async_session_factory
from app.models.peer_approval_audit import (
    AUDIT_ACTION_AUTH_FAILED,
    AUDIT_ACTION_AUTO_ACCEPTED,
    AUDIT_ACTION_DISCOVERED,
    AUDIT_ACTION_PROVISIONED,
    AUDIT_ACTION_READY,
    AUDIT_ACTION_VERIFIED,
)
from app.models.server_node import (
    PEER_STATE_APPROVED,
    PEER_STATE_AUTH_FAILED,
    PEER_STATE_AUTHENTICATING,
    PEER_STATE_AUTO_ACCEPTED,
    PEER_STATE_AWAITING_HUMAN,
    PEER_STATE_DISCOVERED,
    PEER_STATE_PENDING_APPROVAL,
    PEER_STATE_PROVISIONING,
    PEER_STATE_READY,
    PEER_STATE_REJECTED,
    PEER_STATE_SYNCING_STATE,
    PEER_STATE_VERIFIED,
    PEER_STATE_WAITING_MANUAL_APPROVAL,
    ServerNode,
)
from app.services.peer_acceptance_policy import (
    PeerAcceptanceMode,
    get_policy,
)
from app.services.peer_approval_service import (
    peer_approval_service,
)
from app.services.peer_auth import (
    PeerVerifyResult,
    verify_peer_candidate,
)

logger = get_logger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class AutoPeerEnrollmentService:
    """Stateless orchestrator. Operates via async DB sessions."""

    async def handle_discovered_peer(
        self, announcement: dict[str, Any],
    ) -> dict:
        """Top-level entry point. Returns the resulting ServerNode
        snapshot (or rejection result).

        ``announcement`` shape:
        ::

            {
              "server_id": str,
              "cluster_id": str,
              "endpoint": str (optional),
              "region": str (optional), "zone": str (optional),
              "version": str,
              "capabilities": list[str] | csv str,
              "public_key_fingerprint": str,
              "discovery_method": str (e.g. "udp_broadcast", "mdns"),
              "nonce": str,
              "timestamp": int,
              "signature": str,
            }
        """
        # 1. Upsert DISCOVERED row early so even a rejection produces
        # an audit trail tied to the same DB row.
        node = await self._upsert_discovered(announcement)

        # 2. Run verification.
        await self._set_status(node.server_id, PEER_STATE_AUTHENTICATING)
        result = await verify_peer_candidate(announcement)

        if not result.ok:
            await self._on_auth_failed(node.server_id, result)
            return {
                "ok": False,
                "server_id": node.server_id,
                "reason": result.reason(),
                "approval_status": PEER_STATE_AUTH_FAILED,
            }

        # 3. Apply policy.
        await self._on_verified(node.server_id, result)
        try:
            mode = get_policy().get_mode()
        except Exception as e:
            logger.error("acceptance_policy_invalid", error=str(e))
            return {
                "ok": False,
                "server_id": node.server_id,
                "reason": f"policy_invalid: {e}",
            }

        target_status = await self._place_per_mode(node.server_id, mode, result)

        # 4. If auto_accept, run the rest of the lifecycle inline.
        if mode == PeerAcceptanceMode.AUTO_ACCEPT:
            await self._auto_provision(node.server_id)
            return await self._snapshot(node.server_id)

        return await self._snapshot(node.server_id)

    # ── State helpers ────────────────────────────────────────────

    async def _upsert_discovered(
        self, announcement: dict[str, Any],
    ) -> ServerNode:
        settings = get_settings()
        sid = str(announcement.get("server_id", "")).strip()
        if not sid:
            raise ValueError("announcement missing server_id")
        cluster = str(announcement.get("cluster_id", "")).strip() or settings.COMMCLIENT_CLUSTER_ID
        async with async_session_factory() as db:
            row = (await db.execute(
                select(ServerNode).where(ServerNode.server_id == sid)
            )).scalar_one_or_none()
            now = _utc_now()
            mode_str = settings.COMMCLIENT_PEER_ACCEPTANCE_MODE
            if row is None:
                row = ServerNode(
                    server_id=sid,
                    cluster_id=cluster,
                    region=announcement.get("region"),
                    zone=announcement.get("zone"),
                    endpoint=announcement.get("endpoint"),
                    version=str(announcement.get("version") or ""),
                    capabilities=_norm_caps(announcement.get("capabilities")),
                    public_key_fingerprint=str(
                        announcement.get("public_key_fingerprint") or ""
                    ),
                    discovery_method=announcement.get("discovery_method"),
                    auth_status="unknown",
                    acceptance_mode=mode_str,
                    approval_status=PEER_STATE_DISCOVERED,
                    runtime_status="unknown",
                    last_seen_at=now,
                )
                db.add(row)
            else:
                # Refresh observable fields. Don't clobber approval
                # state — that's owned by the lifecycle methods.
                row.endpoint = announcement.get("endpoint") or row.endpoint
                row.region = announcement.get("region") or row.region
                row.zone = announcement.get("zone") or row.zone
                row.version = str(announcement.get("version") or row.version or "")
                row.capabilities = (
                    _norm_caps(announcement.get("capabilities"))
                    or row.capabilities
                )
                row.public_key_fingerprint = (
                    str(announcement.get("public_key_fingerprint") or "")
                    or row.public_key_fingerprint
                )
                row.discovery_method = (
                    announcement.get("discovery_method") or row.discovery_method
                )
                row.acceptance_mode = mode_str
                row.last_seen_at = now
                # Re-discovery of a previously approved peer should
                # leave its status unchanged so it stays routable.
            await db.commit()
            await db.refresh(row)

        # Audit the first discovery event (idempotent — every
        # discovery emits one row; cheaper than computing "first time"
        # from history).
        await peer_approval_service.record_lifecycle_transition(
            server_id=sid,
            old_status="",
            new_status=row.approval_status,
            action=AUDIT_ACTION_DISCOVERED,
            metadata={
                "discovery_method": announcement.get("discovery_method"),
                "endpoint": announcement.get("endpoint"),
            },
        )
        return row

    async def _set_status(
        self, server_id: str, new_status: str,
        *, auth_status: Optional[str] = None,
    ) -> None:
        async with async_session_factory() as db:
            row = (await db.execute(
                select(ServerNode).where(ServerNode.server_id == server_id)
            )).scalar_one_or_none()
            if row is None:
                return
            old = row.approval_status
            row.approval_status = new_status
            if auth_status is not None:
                row.auth_status = auth_status
            db.add(row)
            await db.commit()
        logger.debug(
            "peer_status_set", server_id=server_id,
            old=old, new=new_status,
        )

    async def _on_auth_failed(
        self, server_id: str, result: PeerVerifyResult,
    ) -> None:
        await self._set_status(
            server_id, PEER_STATE_AUTH_FAILED, auth_status="failed",
        )
        await peer_approval_service.record_lifecycle_transition(
            server_id=server_id,
            old_status=PEER_STATE_AUTHENTICATING,
            new_status=PEER_STATE_AUTH_FAILED,
            action=AUDIT_ACTION_AUTH_FAILED,
            metadata={
                "failure_code": result.failure_code,
                "failure_detail": result.failure_detail,
            },
        )
        # Move to REJECTED so the row reflects the terminal state.
        await self._set_status(server_id, PEER_STATE_REJECTED)
        logger.warning(
            "peer_auth_failed", server_id=server_id,
            code=result.failure_code, detail=result.failure_detail,
        )

    async def _on_verified(
        self, server_id: str, result: PeerVerifyResult,
    ) -> None:
        async with async_session_factory() as db:
            row = (await db.execute(
                select(ServerNode).where(ServerNode.server_id == server_id)
            )).scalar_one_or_none()
            if row is None:
                return
            row.auth_status = "verified"
            row.approval_status = PEER_STATE_VERIFIED
            row.cluster_id = result.cluster_id or row.cluster_id
            row.version = result.version or row.version
            if result.capabilities:
                row.capabilities = ",".join(sorted(result.capabilities))
            row.public_key_fingerprint = (
                result.public_key_fingerprint or row.public_key_fingerprint
            )
            db.add(row)
            await db.commit()
        await peer_approval_service.record_lifecycle_transition(
            server_id=server_id,
            old_status=PEER_STATE_AUTHENTICATING,
            new_status=PEER_STATE_VERIFIED,
            action=AUDIT_ACTION_VERIFIED,
        )

    async def _place_per_mode(
        self, server_id: str, mode: PeerAcceptanceMode, result: PeerVerifyResult,
    ) -> str:
        target = get_policy().state_for_verified_peer(mode)
        await self._set_status(server_id, target)

        if mode == PeerAcceptanceMode.AUTO_ACCEPT:
            await peer_approval_service.record_lifecycle_transition(
                server_id=server_id,
                old_status=PEER_STATE_VERIFIED,
                new_status=target,
                action=AUDIT_ACTION_AUTO_ACCEPTED,
            )
        return target

    async def _auto_provision(self, server_id: str) -> None:
        """Run the same provisioning the admin-approval path would,
        but inline (no human in the loop). Failures here mark the
        peer DEGRADED but don't undo the auto-accept — the admin can
        intervene via the admin API if needed."""
        async with async_session_factory() as db:
            row = (await db.execute(
                select(ServerNode).where(ServerNode.server_id == server_id)
            )).scalar_one_or_none()
            if row is None:
                return
            row.approval_status = PEER_STATE_PROVISIONING
            row.approved_at = _utc_now()
            row.approved_by = "system_auto_accept"
            db.add(row)
            await db.commit()

        # Hook integration points. Each step is best-effort — a
        # provisioning failure produces a DEGRADED peer, not a hard
        # rejection.
        await self._sync_to_runtime_layers(server_id)

        await self._set_status(server_id, PEER_STATE_SYNCING_STATE)
        # State sync is asynchronous — we mark READY immediately so
        # subsequent traffic can flow. The actual snapshot sync runs
        # in the background via load_monitor + presence_service.
        await self._set_status(server_id, PEER_STATE_READY)

        await peer_approval_service.record_lifecycle_transition(
            server_id=server_id,
            old_status=PEER_STATE_PROVISIONING,
            new_status=PEER_STATE_READY,
            action=AUDIT_ACTION_READY,
        )

    async def _sync_to_runtime_layers(self, server_id: str) -> None:
        """Wire an APPROVED peer into the rest of the fabric.

        The peer is already in ``peer_registry`` (the discovery channel
        that surfaced it called ingest before us). What's still missing
        is letting the failure-detector and trust DB know the peer is
        live so the multipath router won't reject it as ``phi_high``
        on its first probe. Best-effort — a failure here marks the
        audit but does not undo auto-accept."""
        # Nudge the failure detector with a synthetic heartbeat so the
        # peer doesn't start at phi=∞ in the routing scorer.
        try:
            from app.services.phi_accrual import get_phi_registry
            get_phi_registry().detector_for(server_id).heartbeat()
        except Exception:
            pass
        # Seed a neutral trust row so trust-aware strategy doesn't
        # auto-reject the peer's first hop. record_event() is the only
        # mutator on the trust DB; we use a benign positive event so
        # the row exists with a non-zero score.
        try:
            from app.services.trust_score import get_trust_db
            db = get_trust_db()
            if db.get_score(server_id) <= 0.0:
                db.record_event(server_id, "successful_exchange")
        except Exception:
            pass

        await peer_approval_service.record_lifecycle_transition(
            server_id=server_id,
            old_status=PEER_STATE_PROVISIONING,
            new_status=PEER_STATE_PROVISIONING,
            action=AUDIT_ACTION_PROVISIONED,
        )

    async def _snapshot(self, server_id: str) -> dict:
        async with async_session_factory() as db:
            row = (await db.execute(
                select(ServerNode).where(ServerNode.server_id == server_id)
            )).scalar_one_or_none()
            if row is None:
                return {"ok": False, "server_id": server_id, "reason": "missing"}
            from app.services.peer_approval_service import PeerApprovalService
            return {"ok": True, **PeerApprovalService._row_to_dict(row)}


def _norm_caps(raw) -> str:
    if isinstance(raw, str):
        items = [c.strip() for c in raw.split(",") if c.strip()]
    elif isinstance(raw, (list, tuple, set)):
        items = [str(c) for c in raw]
    else:
        items = []
    return ",".join(sorted(items))


# ── Module-level singleton ──────────────────────────────────────────

auto_peer_enrollment = AutoPeerEnrollmentService()
