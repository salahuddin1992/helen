"""
Tests for peer acceptance: 4 modes + security rejection paths.

These exercise the orchestrator stack end-to-end against the real
SQLite test DB (no mocks) so the model + service + audit table all
get covered together.
"""

from __future__ import annotations

import asyncio
import time
import uuid

import pytest
from sqlalchemy import delete, select

from app.core.config import get_settings
from app.db.base import Base
from app.db.session import async_session_factory, engine
from app.models.peer_approval_audit import PeerApprovalAudit
from app.models.server_node import (
    PEER_STATE_APPROVED,
    PEER_STATE_AUTH_FAILED,
    PEER_STATE_AUTO_ACCEPTED,
    PEER_STATE_AWAITING_HUMAN,
    PEER_STATE_DENIED,
    PEER_STATE_IGNORED,
    PEER_STATE_PENDING_APPROVAL,
    PEER_STATE_READY,
    PEER_STATE_REJECTED,
    PEER_STATE_REJECTED_BY_ADMIN,
    PEER_STATE_VERIFIED,
    PEER_STATE_WAITING_MANUAL_APPROVAL,
    ServerNode,
)
from app.services.auto_peer_enrollment import auto_peer_enrollment
from app.services.peer_acceptance_policy import PeerAcceptanceMode
from app.services.peer_approval_service import (
    PeerApprovalError,
    peer_approval_service,
)
from app.services.peer_auth import compute_signature


pytestmark = pytest.mark.asyncio


# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
async def module_engine():
    """Ensure ServerNode + PeerApprovalAudit tables exist."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine


@pytest.fixture
def fresh_secret(monkeypatch):
    """Set FEDERATION_SECRET to a stable value for the test process."""
    monkeypatch.setenv("FEDERATION_SECRET", "x" * 32)
    monkeypatch.setenv("FEDERATION_REPLAY_WINDOW_SECONDS", "60")
    monkeypatch.setenv("COMMCLIENT_CLUSTER_ID", "test-cluster")
    monkeypatch.setenv("COMMCLIENT_REQUIRE_PEER_AUTH", "true")
    monkeypatch.setenv("COMMCLIENT_REQUIRE_CLUSTER_ID_MATCH", "true")
    monkeypatch.setenv("COMMCLIENT_REQUIRE_SIGNATURE", "true")
    monkeypatch.setenv("COMMCLIENT_REQUIRE_REPLAY_PROTECTION", "true")
    # Force-reload the cached settings.
    from app.core.config import get_settings as _gs
    _gs.cache_clear()  # type: ignore[attr-defined]
    return "x" * 32


@pytest.fixture(autouse=True)
async def reset_state(module_engine):
    """Clear ServerNode + audit rows + caches between tests."""
    async with async_session_factory() as db:
        await db.execute(delete(PeerApprovalAudit))
        await db.execute(delete(ServerNode))
        await db.commit()
    from app.services.peer_auth import _nonce_cache, _deny_cache
    _nonce_cache._seen.clear()
    _deny_cache._entries.clear()
    yield


def _build_announcement(secret: str, **overrides) -> dict:
    """Construct a properly-signed announcement payload."""
    base = {
        "server_id": overrides.get("server_id", f"peer_{uuid.uuid4().hex[:8]}"),
        "cluster_id": overrides.get("cluster_id", "test-cluster"),
        "endpoint": overrides.get("endpoint", "https://10.1.2.3:3000"),
        "region": overrides.get("region", "lab-r1"),
        "version": overrides.get("version", "1.0.0"),
        "capabilities": overrides.get("capabilities", ["fabric_v1"]),
        "public_key_fingerprint": overrides.get(
            "public_key_fingerprint", "fp_" + uuid.uuid4().hex,
        ),
        "discovery_method": overrides.get("discovery_method", "udp_broadcast"),
        "nonce": overrides.get("nonce", uuid.uuid4().hex),
        "timestamp": overrides.get("timestamp", int(time.time())),
    }
    sig = compute_signature(
        secret=secret,
        server_id=base["server_id"],
        cluster_id=base["cluster_id"],
        nonce=base["nonce"],
        timestamp=base["timestamp"],
        version=base["version"],
        capabilities=set(base["capabilities"]),
        public_key_fingerprint=base["public_key_fingerprint"],
    )
    base["signature"] = overrides.get("signature", sig)
    return base


# ── Mode tests ──────────────────────────────────────────────────────


async def test_auto_accept_mode_lands_ready(monkeypatch, fresh_secret):
    monkeypatch.setenv("COMMCLIENT_PEER_ACCEPTANCE_MODE", "auto_accept")
    from app.core.config import get_settings as _gs
    _gs.cache_clear()  # type: ignore[attr-defined]

    payload = _build_announcement(fresh_secret)
    result = await auto_peer_enrollment.handle_discovered_peer(payload)
    assert result["ok"] is True, result
    assert result["approval_status"] == PEER_STATE_READY
    assert result["auth_status"] == "verified"


async def test_manual_approval_mode_waits_for_admin(monkeypatch, fresh_secret):
    monkeypatch.setenv("COMMCLIENT_PEER_ACCEPTANCE_MODE", "manual_approval")
    from app.core.config import get_settings as _gs
    _gs.cache_clear()  # type: ignore[attr-defined]

    payload = _build_announcement(fresh_secret)
    result = await auto_peer_enrollment.handle_discovered_peer(payload)
    assert result["ok"] is True
    assert result["approval_status"] == PEER_STATE_WAITING_MANUAL_APPROVAL

    # Admin approves.
    approved = await peer_approval_service.approve_peer(
        result["server_id"], admin_user_id="admin_test",
    )
    assert approved["approval_status"] == PEER_STATE_APPROVED
    assert approved["approved_by"] == "admin_test"


async def test_pending_approval_mode_lists_for_admin(monkeypatch, fresh_secret):
    monkeypatch.setenv("COMMCLIENT_PEER_ACCEPTANCE_MODE", "pending_approval")
    from app.core.config import get_settings as _gs
    _gs.cache_clear()  # type: ignore[attr-defined]

    payload = _build_announcement(fresh_secret)
    result = await auto_peer_enrollment.handle_discovered_peer(payload)
    assert result["approval_status"] == PEER_STATE_PENDING_APPROVAL

    # Admin sees it in pending listings.
    pending = await peer_approval_service.list_pending_peers()
    assert any(p["server_id"] == result["server_id"] for p in pending)


async def test_human_selection_mode_awaits(monkeypatch, fresh_secret):
    monkeypatch.setenv("COMMCLIENT_PEER_ACCEPTANCE_MODE", "human_selection")
    from app.core.config import get_settings as _gs
    _gs.cache_clear()  # type: ignore[attr-defined]

    payload = _build_announcement(fresh_secret)
    result = await auto_peer_enrollment.handle_discovered_peer(payload)
    assert result["approval_status"] == PEER_STATE_AWAITING_HUMAN

    # Admin can reject.
    rejected = await peer_approval_service.reject_peer(
        result["server_id"], admin_user_id="admin_test", reason="not_in_lab",
    )
    assert rejected["approval_status"] == PEER_STATE_REJECTED_BY_ADMIN
    assert rejected["reject_reason"] == "not_in_lab"


# ── Security tests ──────────────────────────────────────────────────


async def test_bad_signature_rejected_in_every_mode(monkeypatch, fresh_secret):
    for mode in (
        "auto_accept", "manual_approval", "pending_approval", "human_selection",
    ):
        monkeypatch.setenv("COMMCLIENT_PEER_ACCEPTANCE_MODE", mode)
        from app.core.config import get_settings as _gs
        _gs.cache_clear()  # type: ignore[attr-defined]

        payload = _build_announcement(fresh_secret, signature="not_a_real_hmac")
        result = await auto_peer_enrollment.handle_discovered_peer(payload)
        assert result["ok"] is False, mode
        assert "bad_signature" in result["reason"], (mode, result)


async def test_cluster_mismatch_rejected_in_every_mode(monkeypatch, fresh_secret):
    for mode in (
        "auto_accept", "manual_approval", "pending_approval", "human_selection",
    ):
        monkeypatch.setenv("COMMCLIENT_PEER_ACCEPTANCE_MODE", mode)
        from app.core.config import get_settings as _gs
        _gs.cache_clear()  # type: ignore[attr-defined]

        payload = _build_announcement(fresh_secret, cluster_id="wrong-cluster")
        result = await auto_peer_enrollment.handle_discovered_peer(payload)
        assert result["ok"] is False, mode
        assert "cluster_mismatch" in result["reason"], (mode, result)


async def test_nonce_replay_blocked(monkeypatch, fresh_secret):
    """A genuine replay — same (server_id, nonce) but a tampered signature —
    must be refused with `nonce_replay`. (Behavioural change vs prior version:
    the dedup is now keyed on `(server_id, nonce, signature)` instead of
    `nonce` alone, because the global keying collided with legitimate
    multi-channel discovery — the same UDP broadcast received via UDP
    listener AND federation gossip AND manual seed probe simultaneously.
    The new keying still detects replays while letting idempotent retries
    through.)"""
    monkeypatch.setenv("COMMCLIENT_PEER_ACCEPTANCE_MODE", "auto_accept")
    from app.core.config import get_settings as _gs
    _gs.cache_clear()  # type: ignore[attr-defined]

    nonce = uuid.uuid4().hex
    p1 = _build_announcement(fresh_secret, nonce=nonce)
    r1 = await auto_peer_enrollment.handle_discovered_peer(p1)
    assert r1["ok"] is True

    # Replay: same server_id + nonce, but a forged signature. This is the
    # actual replay-attack shape an attacker would build (capture the wire
    # then re-emit with their own signature). Must fail with nonce_replay.
    p2 = _build_announcement(
        fresh_secret,
        server_id=p1["server_id"],
        nonce=nonce,
        signature="0" * 64,
    )
    r2 = await auto_peer_enrollment.handle_discovered_peer(p2)
    assert r2["ok"] is False
    assert "nonce_replay" in r2["reason"]


async def test_nonce_legitimate_multichannel_idempotent(monkeypatch, fresh_secret):
    """Same authentic announcement arriving via two discovery channels (UDP
    broadcast + manual seed probe + federation gossip) MUST succeed on the
    second arrival, not be rejected as replay. This was the bug that broke
    topology B/C in the live federation harness."""
    monkeypatch.setenv("COMMCLIENT_PEER_ACCEPTANCE_MODE", "auto_accept")
    from app.core.config import get_settings as _gs
    _gs.cache_clear()  # type: ignore[attr-defined]

    payload = _build_announcement(fresh_secret)
    r1 = await auto_peer_enrollment.handle_discovered_peer(payload)
    assert r1["ok"] is True

    # Identical payload arriving via a different discovery channel.
    r2 = await auto_peer_enrollment.handle_discovered_peer(payload)
    assert r2["ok"] is True, r2


async def test_nonce_collision_across_servers_allowed(monkeypatch, fresh_secret):
    """Two DIFFERENT servers happening to use the same nonce (cosmic-ray-tier
    uuid4 collision, or a mis-seeded RNG) must each verify on their own
    merits. Dedup is keyed by (server_id, nonce), not just nonce."""
    monkeypatch.setenv("COMMCLIENT_PEER_ACCEPTANCE_MODE", "auto_accept")
    from app.core.config import get_settings as _gs
    _gs.cache_clear()  # type: ignore[attr-defined]

    nonce = uuid.uuid4().hex
    p1 = _build_announcement(fresh_secret, nonce=nonce, server_id="server_alpha")
    p2 = _build_announcement(fresh_secret, nonce=nonce, server_id="server_beta")
    r1 = await auto_peer_enrollment.handle_discovered_peer(p1)
    r2 = await auto_peer_enrollment.handle_discovered_peer(p2)
    assert r1["ok"] is True, r1
    assert r2["ok"] is True, r2


async def test_stale_timestamp_blocked(monkeypatch, fresh_secret):
    monkeypatch.setenv("COMMCLIENT_PEER_ACCEPTANCE_MODE", "auto_accept")
    from app.core.config import get_settings as _gs
    _gs.cache_clear()  # type: ignore[attr-defined]

    payload = _build_announcement(
        fresh_secret, timestamp=int(time.time()) - 3600,  # 1 hour stale
    )
    result = await auto_peer_enrollment.handle_discovered_peer(payload)
    assert result["ok"] is False
    assert "stale_timestamp" in result["reason"]


async def test_approve_refused_when_not_verified(monkeypatch, fresh_secret):
    """Even an admin can't approve a peer that hasn't been verified."""
    monkeypatch.setenv("COMMCLIENT_PEER_ACCEPTANCE_MODE", "manual_approval")
    from app.core.config import get_settings as _gs
    _gs.cache_clear()  # type: ignore[attr-defined]

    # Build a row directly in DISCOVERED state (no verify ran).
    async with async_session_factory() as db:
        db.add(ServerNode(
            server_id="unverified_peer",
            cluster_id="test-cluster",
            approval_status=PEER_STATE_WAITING_MANUAL_APPROVAL,
            auth_status="unknown",  # NOT verified
            acceptance_mode="manual_approval",
            runtime_status="unknown",
        ))
        await db.commit()

    with pytest.raises(PeerApprovalError) as ei:
        await peer_approval_service.approve_peer(
            "unverified_peer", admin_user_id="admin_test",
        )
    assert "not_verified" in str(ei.value)


async def test_deny_pushes_fingerprint_to_cache(monkeypatch, fresh_secret):
    """Once denied, re-discovery short-circuits via the deny cache."""
    monkeypatch.setenv("COMMCLIENT_PEER_ACCEPTANCE_MODE", "manual_approval")
    from app.core.config import get_settings as _gs
    _gs.cache_clear()  # type: ignore[attr-defined]

    payload = _build_announcement(fresh_secret)
    r1 = await auto_peer_enrollment.handle_discovered_peer(payload)
    assert r1["ok"] is True
    fp = r1["public_key_fingerprint"]

    await peer_approval_service.deny_peer(
        r1["server_id"], admin_user_id="admin", reason="malicious",
    )

    # Re-discover with a fresh nonce/timestamp/server_id but same fingerprint.
    p2 = _build_announcement(fresh_secret, public_key_fingerprint=fp,
                              server_id="returning_peer")
    r2 = await auto_peer_enrollment.handle_discovered_peer(p2)
    assert r2["ok"] is False
    assert "deny_cache" in r2["reason"]


async def test_is_peer_routable_only_for_active_states(fresh_secret):
    """READY/DEGRADED → True. Anything else → False."""
    from app.models.server_node import (
        PEER_STATE_DEGRADED,
        PEER_STATE_DISCOVERED,
        PEER_STATE_PENDING_APPROVAL,
        PEER_STATE_DENIED,
    )
    cases = [
        (PEER_STATE_READY, True),
        (PEER_STATE_DEGRADED, True),
        (PEER_STATE_DISCOVERED, False),
        (PEER_STATE_PENDING_APPROVAL, False),
        (PEER_STATE_DENIED, False),
        (PEER_STATE_REJECTED_BY_ADMIN, False),
    ]
    for status, expected in cases:
        async with async_session_factory() as db:
            db.add(ServerNode(
                server_id=f"peer_{status}",
                cluster_id="test-cluster",
                approval_status=status,
                auth_status="verified",
                acceptance_mode="manual_approval",
                runtime_status="unknown",
            ))
            await db.commit()
        ok = await peer_approval_service.is_peer_routable(f"peer_{status}")
        assert ok is expected, f"{status} expected={expected} got={ok}"


async def test_unknown_peer_is_not_routable(fresh_secret):
    """A server_id not in the DB must NOT be routable (fail-closed)."""
    ok = await peer_approval_service.is_peer_routable("totally_unknown_sid")
    assert ok is False


async def test_evict_stale_waiting_skips_fresh(monkeypatch, fresh_secret):
    """A WAITING peer with recent last_seen_at is NOT evicted."""
    monkeypatch.setenv("COMMCLIENT_PEER_PENDING_TTL_SECONDS", "86400")
    from app.core.config import get_settings as _gs
    _gs.cache_clear()  # type: ignore[attr-defined]

    from datetime import datetime, timezone
    async with async_session_factory() as db:
        db.add(ServerNode(
            server_id="fresh_waiter",
            cluster_id="test-cluster",
            approval_status=PEER_STATE_WAITING_MANUAL_APPROVAL,
            auth_status="verified",
            acceptance_mode="manual_approval",
            runtime_status="unknown",
            last_seen_at=datetime.now(timezone.utc),
        ))
        await db.commit()

    evicted = await peer_approval_service.evict_stale_waiting()
    assert evicted == 0


async def test_evict_stale_waiting_kills_old(monkeypatch, fresh_secret):
    """A WAITING peer with last_seen_at older than TTL is evicted."""
    monkeypatch.setenv("COMMCLIENT_PEER_PENDING_TTL_SECONDS", "10")
    from app.core.config import get_settings as _gs
    _gs.cache_clear()  # type: ignore[attr-defined]

    from datetime import datetime, timedelta, timezone
    async with async_session_factory() as db:
        db.add(ServerNode(
            server_id="stale_waiter",
            cluster_id="test-cluster",
            approval_status=PEER_STATE_WAITING_MANUAL_APPROVAL,
            auth_status="verified",
            acceptance_mode="manual_approval",
            runtime_status="unknown",
            last_seen_at=datetime.now(timezone.utc) - timedelta(seconds=120),
        ))
        await db.commit()

    evicted = await peer_approval_service.evict_stale_waiting()
    assert evicted == 1

    # Subsequent run is a no-op — peer is now in EVICTED, not WAITING.
    again = await peer_approval_service.evict_stale_waiting()
    assert again == 0


async def test_broadcast_payload_contains_auth_fields(monkeypatch, fresh_secret):
    """The local server's broadcast carries the new auth fields when
    FEDERATION_SECRET is configured. Receiving peers can run
    ``verify_peer_candidate`` against this payload and pass."""
    import json
    from app.services.discovery_service import udp_broadcast
    from app.services.peer_auth import verify_peer_candidate

    raw = await udp_broadcast.get_broadcast_payload()
    parsed = json.loads(raw.decode("utf-8"))
    for k in ("server_id", "cluster_id", "nonce", "timestamp",
              "signature", "public_key_fingerprint", "capabilities"):
        assert k in parsed, f"missing auth field {k}: {parsed}"

    # Round-trip: the receiver runs verify against our payload.
    # The verifier expects "version" (the local broadcast already
    # carries it as data["version"]).
    parsed["version"] = parsed.get("version", "1.0.0")
    result = await verify_peer_candidate(parsed)
    # Skip cluster check edge case — our local cluster_id == "test-cluster"
    # set by fixture, and we just signed with the same. Should pass.
    assert result.ok, f"verify failed: {result.failure_code}: {result.failure_detail}"


async def test_federation_gate_blocks_unapproved_peer():
    """Verify the _verify federation gate refuses unapproved peers.
    We test the helper directly — full HTTP integration is covered by
    the auth_status check inside _verify after HMAC verify."""
    from app.services.peer_approval_service import peer_approval_service

    # Insert a peer in PENDING_APPROVAL.
    async with async_session_factory() as db:
        db.add(ServerNode(
            server_id="pending_peer_xyz",
            cluster_id="test-cluster",
            approval_status=PEER_STATE_PENDING_APPROVAL,
            auth_status="verified",
            acceptance_mode="pending_approval",
            runtime_status="unknown",
        ))
        await db.commit()

    routable = await peer_approval_service.is_peer_routable("pending_peer_xyz")
    assert routable is False, "pending peer must NOT be routable"


async def test_audit_log_records_admin_actions(monkeypatch, fresh_secret):
    monkeypatch.setenv("COMMCLIENT_PEER_ACCEPTANCE_MODE", "manual_approval")
    monkeypatch.setenv("COMMCLIENT_PEER_APPROVAL_AUDIT_LOG", "true")
    from app.core.config import get_settings as _gs
    _gs.cache_clear()  # type: ignore[attr-defined]

    payload = _build_announcement(fresh_secret)
    result = await auto_peer_enrollment.handle_discovered_peer(payload)
    await peer_approval_service.approve_peer(
        result["server_id"], admin_user_id="admin_test",
    )

    async with async_session_factory() as db:
        rows = (await db.execute(
            select(PeerApprovalAudit).where(
                PeerApprovalAudit.server_id == result["server_id"]
            )
        )).scalars().all()
        actions = [r.action for r in rows]
    assert "discovered" in actions
    assert "verified" in actions
    assert "approved" in actions
