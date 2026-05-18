"""
Unit tests for hybrid call-topology subsystem.

Covers three collaborating modules:

  * ``app.services.topology_manager``      — routing policy, cooldown,
                                              generation bump, SFU allocate
                                              fallback
  * ``app.services.call_state_persistence`` — signal replay truncation,
                                              orphan sweeper, heartbeat
  * ``app.socket.topology_handlers``       — auth gate / participant gate
                                              for the socket events, via
                                              direct function calls (the
                                              sio session is monkey-patched).

The tests avoid spinning up the real Socket.IO server — they invoke the
handler coroutines directly after stubbing ``sio.get_session`` and
``sio.emit``. That keeps the surface of the test focused on the actual
topology logic instead of Socket.IO plumbing.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from app.db.base import Base
from app.db.session import async_session_factory, engine
from app.models.active_call import (
    ActiveCall,
    ActiveCallParticipant,
    CallSignalEvent,
)
from app.models.user import User
from app.core.security import hash_password
from app.services.call_state_persistence import (
    HEARTBEAT_STALE_SECONDS,
    SIGNAL_REPLAY_LIMIT,
    CallStatePersistence,
)
from app.services.topology_manager import (
    MESH_MAX_PARTICIPANTS,
    PACKET_LOSS_FLOOR,
    QUALITY_BAD_RATIO,
    RTT_BAD_MS,
    NoopSFU,
    QualityOracle,
    QualitySample,
    SFUBackend,
    TopologyManager,
)


pytestmark = pytest.mark.asyncio


# ─────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
async def module_engine():
    """Create schema once for the whole topology test module."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine


@pytest.fixture
async def user(module_engine):
    async with async_session_factory() as s:
        u = User(
            id=uuid.uuid4().hex,
            username=f"topology-{uuid.uuid4().hex[:8]}",
            display_name="Topology User",
            password_hash=hash_password("x"),
            status="online",
        )
        s.add(u)
        await s.commit()
        await s.refresh(u)
        return u


@pytest.fixture
async def second_user(module_engine):
    async with async_session_factory() as s:
        u = User(
            id=uuid.uuid4().hex,
            username=f"topology-b-{uuid.uuid4().hex[:8]}",
            display_name="Topology User B",
            password_hash=hash_password("x"),
            status="online",
        )
        s.add(u)
        await s.commit()
        await s.refresh(u)
        return u


def _make_fake_call(
    *, call_id: str, participants: list[str], routing: str = "mesh",
) -> Any:
    """
    Build a lightweight stand-in for ``call_service.ActiveCall``.

    topology_manager only pokes ``.routing``, ``.call_id``, and
    ``.participants`` (.keys() iterable of user_ids), so a SimpleNamespace
    is enough.
    """
    return SimpleNamespace(
        call_id=call_id,
        routing=routing,
        participants={uid: {} for uid in participants},
    )


class _FakeBackend(SFUBackend):
    """SFU backend that records allocate/release and fails on demand."""

    def __init__(self, *, fail: bool = False) -> None:
        self.name = "fake"
        self.fail = fail
        self.allocated: list[str] = []
        self.released: list[str] = []

    async def allocate_router(self, call_id: str) -> dict:
        if self.fail:
            raise RuntimeError("mediasoup worker unavailable")
        self.allocated.append(call_id)
        return {"backend": "fake", "url": f"rtc://fake/{call_id}", "producer_token": "t"}

    async def release_router(self, call_id: str) -> None:
        self.released.append(call_id)


# ─────────────────────────────────────────────────────────────────────
# Pure policy — no DB, no sockets
# ─────────────────────────────────────────────────────────────────────


async def test_desired_routing_boundaries():
    tm = TopologyManager(backend=_FakeBackend())
    assert tm.desired_routing(1) == "p2p"
    assert tm.desired_routing(2) == "p2p"
    assert tm.desired_routing(3) == "mesh"
    assert tm.desired_routing(MESH_MAX_PARTICIPANTS) == "mesh"
    assert tm.desired_routing(MESH_MAX_PARTICIPANTS + 1) == "sfu"
    assert tm.desired_routing(100) == "sfu"


async def test_quality_oracle_bad_ratio():
    q = QualityOracle()
    q.record("c1", "u1", QualitySample(packet_loss=PACKET_LOSS_FLOOR + 0.01))
    q.record("c1", "u2", QualitySample(packet_loss=0.0, rtt_ms=10))
    q.record("c1", "u3", QualitySample(rtt_ms=RTT_BAD_MS + 10))
    # 2 bad / 3 total = 0.66 → above QUALITY_BAD_RATIO (0.4)
    assert q.bad_participants_ratio("c1") > QUALITY_BAD_RATIO
    q.forget("c1")
    assert q.bad_participants_ratio("c1") == 0.0


async def test_quality_upgrade_from_mesh(monkeypatch):
    """
    Small group (3 participants would normally stay mesh), but packet loss
    pushes desired_routing to sfu via the oracle.
    """
    backend = _FakeBackend()
    tm = TopologyManager(backend=backend)
    # Stub out broadcast so we don't need sio
    monkeypatch.setattr(tm, "_broadcast_switch", _noop_broadcast)

    call_id = uuid.uuid4().hex
    call = _make_fake_call(call_id=call_id, participants=["a", "b", "c"], routing="mesh")
    for u in ("a", "b", "c"):
        tm.quality.record(call_id, u, QualitySample(packet_loss=PACKET_LOSS_FLOOR + 0.02))

    new_routing = await tm.reevaluate(call)
    assert new_routing == "sfu"
    assert call.routing == "sfu"
    assert backend.allocated == [call_id]
    assert tm.current_generation(call_id) == 2


async def test_reevaluate_noop_when_already_correct(monkeypatch):
    backend = _FakeBackend()
    tm = TopologyManager(backend=backend)
    monkeypatch.setattr(tm, "_broadcast_switch", _noop_broadcast)

    call = _make_fake_call(call_id="cx", participants=["a", "b"], routing="p2p")
    assert await tm.reevaluate(call) is None
    assert backend.allocated == []


async def test_force_switch_bumps_generation_and_allocates(monkeypatch):
    backend = _FakeBackend()
    tm = TopologyManager(backend=backend)
    monkeypatch.setattr(tm, "_broadcast_switch", _noop_broadcast)

    call_id = uuid.uuid4().hex
    call = _make_fake_call(call_id=call_id, participants=["a", "b", "c"], routing="mesh")

    gen0 = tm.current_generation(call_id)
    new_routing = await tm.force_switch(call, "sfu", reason="manual")
    assert new_routing == "sfu"
    assert tm.current_generation(call_id) == gen0 + 1
    assert backend.allocated == [call_id]

    # Downgrade from SFU releases the router
    await tm.force_switch(call, "mesh", reason="manual")
    assert backend.released == [call_id]
    assert call.routing == "mesh"


async def test_force_switch_downgrades_on_sfu_allocate_failure(monkeypatch):
    """
    If the SFU backend raises on allocate, topology_manager must NOT crash.
    It should downgrade the call to mesh so the call stays alive.
    """
    backend = _FakeBackend(fail=True)
    tm = TopologyManager(backend=backend)
    monkeypatch.setattr(tm, "_broadcast_switch", _noop_broadcast)

    call_id = uuid.uuid4().hex
    call = _make_fake_call(call_id=call_id, participants=["a", "b", "c", "d", "e", "f"], routing="mesh")

    new_routing = await tm.force_switch(call, "sfu", reason="participant_count")
    assert new_routing == "mesh"
    assert call.routing == "mesh"


async def test_cooldown_prevents_flapping(monkeypatch):
    backend = _FakeBackend()
    tm = TopologyManager(backend=backend)
    monkeypatch.setattr(tm, "_broadcast_switch", _noop_broadcast)

    call_id = uuid.uuid4().hex
    # Use 9 participants — the SFU_MIN_PARTICIPANTS threshold. Anything ≤ 8
    # stays in mesh because the iOS web-simulator doesn't support SFU yet.
    call = _make_fake_call(
        call_id=call_id,
        participants=[f"u{i}" for i in range(9)],
        routing="mesh",
    )

    # First switch should proceed (n=9 → sfu).
    res1 = await tm.reevaluate(call)
    assert res1 == "sfu"

    # Now simulate participants dropping back to 3 — would normally mesh.
    call.participants = {"a": {}, "b": {}, "c": {}}
    # Second switch happens immediately after → should be suppressed by cooldown.
    res2 = await tm.reevaluate(call)
    assert res2 is None
    assert call.routing == "sfu"  # unchanged during cooldown


async def test_on_call_ended_releases_sfu(monkeypatch):
    backend = _FakeBackend()
    tm = TopologyManager(backend=backend)
    monkeypatch.setattr(tm, "_broadcast_switch", _noop_broadcast)

    call_id = uuid.uuid4().hex
    call = _make_fake_call(call_id=call_id, participants=["a", "b", "c", "d", "e"], routing="mesh")
    await tm.force_switch(call, "sfu", reason="participant_count")
    await tm.on_call_ended(call_id)
    assert call_id in backend.released
    # All state cleared
    assert tm.current_generation(call_id) == 1


async def test_restore_generation_prevents_rewind():
    """After restart, rehydrate must seed the generation counter."""
    tm = TopologyManager(backend=_FakeBackend())
    call_id = uuid.uuid4().hex

    assert tm.current_generation(call_id) == 1
    tm.restore_generation(call_id, 7)
    assert tm.current_generation(call_id) == 7

    # Idempotent: subsequent calls only raise, never lower
    tm.restore_generation(call_id, 3)
    assert tm.current_generation(call_id) == 7

    # Negative / zero generations ignored
    tm.restore_generation(call_id, 0)
    tm.restore_generation(call_id, -5)
    assert tm.current_generation(call_id) == 7


async def test_mark_router_stale_forces_reallocate(monkeypatch):
    """After restart, marking SFU call stale must force re-allocation on next switch."""
    backend = _FakeBackend()
    tm = TopologyManager(backend=backend)
    monkeypatch.setattr(tm, "_broadcast_switch", _noop_broadcast)

    call_id = uuid.uuid4().hex
    call = _make_fake_call(call_id=call_id, participants=["a", "b", "c", "d", "e"], routing="sfu")
    # Simulate post-restart state: routing=sfu but backend has no router cached
    tm.restore_generation(call_id, 5)
    tm.mark_router_stale(call_id)
    assert call_id not in tm._router_info
    assert tm.current_generation(call_id) == 5

    # Next switch back up to SFU (e.g., after a participant leaves and rejoins)
    # — force_switch to sfu should re-allocate on the backend.
    call.routing = "mesh"  # pretend we demoted for some reason
    await tm.force_switch(call, "sfu", reason="manual")
    assert call_id in backend.allocated
    assert tm.current_generation(call_id) == 6  # bumped from the restored 5


# ─────────────────────────────────────────────────────────────────────
# Persistence: signal replay + truncation + heartbeat sweep
# ─────────────────────────────────────────────────────────────────────


async def test_replay_signals_honest_truncation(user):
    """
    When there are more matching events than SIGNAL_REPLAY_LIMIT, the
    ``truncated`` flag must be ``True`` so the client can fall back to a
    full renegotiate. With ≤ limit, ``truncated`` must be ``False``.
    """
    call_id = uuid.uuid4().hex
    persistence = CallStatePersistence()

    # Bootstrap active_call row (replay requires a valid call_id FK)
    await persistence.upsert_call(
        call_id=call_id,
        initiator_id=user.id,
        call_type="audio",
        routing="mesh",
        channel_id=None,
    )

    # Append SIGNAL_REPLAY_LIMIT + 1 events
    for i in range(SIGNAL_REPLAY_LIMIT + 1):
        await persistence.append_signal(
            call_id=call_id,
            from_user=user.id,
            to_user=None,
            kind="ice",
            payload={"idx": i},
            topology_generation=1,
        )

    events, truncated = await persistence.replay_signals(call_id, for_user=user.id)
    assert truncated is True
    # Cap is honored — never over the limit
    assert len(events) <= SIGNAL_REPLAY_LIMIT

    # Now trim back down and re-test
    await persistence.trim_signals(call_id, keep=10)
    events2, truncated2 = await persistence.replay_signals(call_id, for_user=user.id)
    assert truncated2 is False
    assert len(events2) <= 10


async def test_replay_filters_by_user_and_generation(user, second_user):
    call_id = uuid.uuid4().hex
    p = CallStatePersistence()
    await p.upsert_call(
        call_id=call_id,
        initiator_id=user.id,
        call_type="audio",
        routing="mesh",
        channel_id=None,
    )

    # Offers destined to second_user at gen=1 and gen=2
    await p.append_signal(
        call_id=call_id, from_user=user.id, to_user=second_user.id,
        kind="offer", payload={"sdp": "v=0..."}, topology_generation=1,
    )
    await p.append_signal(
        call_id=call_id, from_user=user.id, to_user=second_user.id,
        kind="answer", payload={"sdp": "v=0..."}, topology_generation=2,
    )
    # Broadcast (to_user=None) — should always appear for every reader
    await p.append_signal(
        call_id=call_id, from_user="server", to_user=None,
        kind="topology", payload={"new_routing": "sfu"}, topology_generation=2,
    )
    # Other-user only — must NOT appear for second_user
    await p.append_signal(
        call_id=call_id, from_user=user.id, to_user="ghost-user",
        kind="offer", payload={}, topology_generation=2,
    )

    events, _ = await p.replay_signals(call_id, for_user=second_user.id)
    kinds = [e["kind"] for e in events]
    # Must include the two offer/answer + topology broadcast = 3
    assert "offer" in kinds
    assert "answer" in kinds
    assert "topology" in kinds
    # Must not leak signals addressed to another user
    for e in events:
        assert e["to"] in (None, second_user.id)

    # since_generation gate
    filtered, _ = await p.replay_signals(
        call_id, for_user=second_user.id, since_generation=2,
    )
    for e in filtered:
        assert e["topology_generation"] >= 2


async def test_sweep_orphans_marks_stale_calls_ended(user):
    call_id = uuid.uuid4().hex
    p = CallStatePersistence()
    await p.upsert_call(
        call_id=call_id,
        initiator_id=user.id,
        call_type="audio",
        routing="mesh",
        channel_id=None,
        status="active",
    )

    # Fake an old heartbeat
    async with async_session_factory() as s:
        call = await s.get(ActiveCall, call_id)
        call.last_heartbeat_at = datetime.now(timezone.utc) - timedelta(
            seconds=HEARTBEAT_STALE_SECONDS + 30,
        )
        call.started_at = datetime.now(timezone.utc) - timedelta(minutes=10)
        await s.commit()

    swept = await p.sweep_orphans()
    # Return type is now list[str] of swept call_ids (see fix: keep DB +
    # in-memory state in sync instead of just returning a count).
    assert isinstance(swept, list)
    assert call_id in swept

    async with async_session_factory() as s:
        call = await s.get(ActiveCall, call_id)
        assert call is not None
        assert call.status == "ended"
        assert call.ended_at is not None
        meta = json.loads(call.metadata_json or "{}")
        assert meta.get("end_reason") == "heartbeat_timeout"


async def test_heartbeat_refreshes_last_heartbeat(user):
    call_id = uuid.uuid4().hex
    p = CallStatePersistence()
    await p.upsert_call(
        call_id=call_id, initiator_id=user.id, call_type="audio",
        routing="mesh", channel_id=None, status="active",
    )

    # Push the heartbeat back in time.
    async with async_session_factory() as s:
        call = await s.get(ActiveCall, call_id)
        call.last_heartbeat_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        await s.commit()

    await p.heartbeat(call_id)

    async with async_session_factory() as s:
        call = await s.get(ActiveCall, call_id)
        # SQLite's DateTime(timezone=True) round-trips as naive; normalize to UTC.
        last_hb = call.last_heartbeat_at
        if last_hb.tzinfo is None:
            last_hb = last_hb.replace(tzinfo=timezone.utc)
        # Must be < 30s old after refresh
        age = (datetime.now(timezone.utc) - last_hb).total_seconds()
        assert age < 30


# ─────────────────────────────────────────────────────────────────────
# Socket handler auth / participant gates
# ─────────────────────────────────────────────────────────────────────
#
# The handlers in ``app.socket.topology_handlers`` rely on
# ``sio.get_session(sid)`` to know who the caller is. Rather than spin up
# a full Socket.IO server we patch that single method.

@pytest.fixture
def patch_sio(monkeypatch):
    """
    Monkey-patch ``sio.get_session``/``sio.emit`` on the topology_handlers
    module so calling the handler coroutine directly behaves like a
    logged-in client.
    """
    from app.socket import server as server_module

    session_map: dict[str, dict] = {}

    async def fake_get_session(sid: str) -> dict:
        return session_map.get(sid, {})

    async def fake_emit(*args, **kwargs):
        return None

    monkeypatch.setattr(server_module.sio, "get_session", fake_get_session)
    monkeypatch.setattr(server_module.sio, "emit", fake_emit)

    def set_session(sid: str, user_id: str) -> None:
        session_map[sid] = {"user_id": user_id}

    return set_session


async def test_heartbeat_handler_rejects_unauthenticated(patch_sio):
    from app.socket.topology_handlers import _on_call_heartbeat
    res = await _on_call_heartbeat("sid-anon", {"call_id": "x"})
    assert res == {"ok": False, "error": "unauthenticated"}


async def test_heartbeat_handler_rejects_missing_call_id(patch_sio, user):
    patch_sio("sid1", user.id)
    from app.socket.topology_handlers import _on_call_heartbeat
    res = await _on_call_heartbeat("sid1", {})
    assert res == {"ok": False, "error": "call_id required"}


async def test_heartbeat_handler_rejects_non_participant(patch_sio, user):
    """
    Even with a valid session, if the user isn't a participant of the call
    the server must reject — a malicious client could otherwise keep any
    call alive indefinitely.
    """
    from app.services.call_service import call_service
    from app.socket.topology_handlers import _on_call_heartbeat

    patch_sio("sid2", user.id)
    res = await _on_call_heartbeat("sid2", {"call_id": "does-not-exist"})
    assert res["ok"] is False
    assert res["error"] == "not in call"


async def test_heartbeat_handler_accepts_participant(patch_sio, user, monkeypatch):
    """
    With a real call + participant, the handler should return ok and
    refresh the DB heartbeat.
    """
    from app.services.call_service import ActiveCall as InMemoryCall, call_service
    from app.socket.topology_handlers import _on_call_heartbeat

    call_id = uuid.uuid4().hex
    # Inject a minimal in-memory call so call_service.get_call resolves.
    mem_call = InMemoryCall(
        call_id=call_id,
        initiator_id=user.id,
        call_type="audio",
        routing="mesh",
    )
    mem_call.add_participant(user.id)
    call_service._active_calls[call_id] = mem_call

    # Also persist it so the heartbeat() write has a row to update.
    p = CallStatePersistence()
    await p.upsert_call(
        call_id=call_id, initiator_id=user.id, call_type="audio",
        routing="mesh", channel_id=None, status="active",
    )

    patch_sio("sid3", user.id)

    try:
        res = await _on_call_heartbeat("sid3", {"call_id": call_id})
        assert res["ok"] is True
        assert isinstance(res.get("server_ts"), int)
    finally:
        call_service._active_calls.pop(call_id, None)


async def test_quality_report_handler_records_and_reevaluates(patch_sio, user, monkeypatch):
    """
    Quality reports must:
      * validate numeric fields (bad values → 400-equivalent)
      * feed the QualityOracle
      * trigger a reevaluate() pass
    """
    from app.services.call_service import ActiveCall as InMemoryCall, call_service
    from app.services.topology_manager import topology_manager
    from app.socket.topology_handlers import _on_quality_report

    call_id = uuid.uuid4().hex
    mem_call = InMemoryCall(
        call_id=call_id, initiator_id=user.id, call_type="video", routing="mesh",
    )
    mem_call.add_participant(user.id)
    call_service._active_calls[call_id] = mem_call

    # Persist so record_quality() has a row
    p = CallStatePersistence()
    await p.upsert_call(
        call_id=call_id, initiator_id=user.id, call_type="video",
        routing="mesh", channel_id=None, status="active",
    )
    await p.add_participant(call_id=call_id, user_id=user.id, sid="sid-q", role="initiator")

    patch_sio("sid-q", user.id)

    reevaluate_calls: list[Any] = []

    async def fake_reevaluate(call):
        reevaluate_calls.append(call)
        return None

    monkeypatch.setattr(topology_manager, "reevaluate", fake_reevaluate)

    try:
        # Invalid (non-numeric) metrics
        bad = await _on_quality_report(
            "sid-q", {"call_id": call_id, "packet_loss": "not-a-number"},
        )
        assert bad == {"ok": False, "error": "invalid metrics"}

        # Valid path
        ok = await _on_quality_report("sid-q", {
            "call_id": call_id, "packet_loss": 0.15, "rtt_ms": 120.0, "jitter_ms": 20.0,
        })
        assert ok == {"ok": True}
        assert len(reevaluate_calls) == 1
        # Oracle recorded the sample
        ratio = topology_manager.quality.bad_participants_ratio(call_id)
        assert ratio > 0.0
    finally:
        call_service._active_calls.pop(call_id, None)
        topology_manager.quality.forget(call_id)


async def test_signal_replay_handler_returns_truncated_flag(patch_sio, user):
    """
    End-to-end: populate > SIGNAL_REPLAY_LIMIT events, call the replay
    handler directly, and confirm the response carries
    ``truncated=True``.
    """
    from app.services.call_service import ActiveCall as InMemoryCall, call_service
    from app.socket.topology_handlers import _on_signal_replay

    call_id = uuid.uuid4().hex
    mem_call = InMemoryCall(
        call_id=call_id, initiator_id=user.id, call_type="audio", routing="mesh",
    )
    mem_call.add_participant(user.id)
    call_service._active_calls[call_id] = mem_call

    p = CallStatePersistence()
    await p.upsert_call(
        call_id=call_id, initiator_id=user.id, call_type="audio",
        routing="mesh", channel_id=None, status="active",
    )
    for i in range(SIGNAL_REPLAY_LIMIT + 5):
        await p.append_signal(
            call_id=call_id, from_user=user.id, to_user=None,
            kind="ice", payload={"i": i}, topology_generation=1,
        )

    patch_sio("sid-r", user.id)
    try:
        res = await _on_signal_replay("sid-r", {"call_id": call_id})
        assert res["ok"] is True
        assert res["truncated"] is True
        assert len(res["signals"]) <= SIGNAL_REPLAY_LIMIT
        assert "generation" in res
        assert "routing" in res
    finally:
        call_service._active_calls.pop(call_id, None)


async def test_topology_request_rejects_invalid_routing(patch_sio, user):
    from app.socket.topology_handlers import _on_topology_request

    patch_sio("sid-t", user.id)
    res = await _on_topology_request("sid-t", {"call_id": "c1", "routing": "carrier-pigeon"})
    assert res == {"ok": False, "error": "invalid params"}


# ─────────────────────────────────────────────────────────────────────
# CallService → TopologyManager integration (router leak fix)
# ─────────────────────────────────────────────────────────────────────


async def test_call_service_hangup_releases_sfu_router(monkeypatch):
    """
    Regression: every call-end path must release the SFU router.
    Before the fix, hangup/leave_call left mediasoup routers allocated.
    """
    from app.services import topology_manager as tm_module
    from app.services.call_service import CallService

    backend = _FakeBackend()
    tm_module.topology_manager = tm_module.TopologyManager(backend=backend)
    monkeypatch.setattr(
        tm_module.topology_manager, "_broadcast_switch", _noop_broadcast
    )

    svc = CallService()
    # Bypass DB persistence side-effects — we only want in-memory + topology.
    import app.services.call_state_persistence as persistence
    for name in (
        "upsert_call",
        "add_participant",
        "mark_active",
        "mark_ended",
        "remove_participant",
        "update_participant_flags",
    ):
        if hasattr(persistence.call_state_persistence, name):
            async def _noop(*a, **k):
                return None
            monkeypatch.setattr(persistence.call_state_persistence, name, _noop)

    uid = uuid.uuid4().hex
    call = await svc.initiate_call(initiator_id=uid, call_type="video", routing="mesh")
    # Force into SFU so the backend allocates a router
    for extra in range(4):
        call.add_participant(f"u{extra}")
    await tm_module.topology_manager.force_switch(
        call, "sfu", reason="participant_count"
    )
    assert call.call_id in backend.allocated

    await svc.hangup(call.call_id, uid)

    # Drain the fire-and-forget release task
    for _ in range(20):
        if call.call_id in backend.released:
            break
        await asyncio.sleep(0.01)

    assert call.call_id in backend.released, "SFU router must be released on hangup"
    assert call.call_id not in svc._active_calls


async def test_call_service_reap_ended_calls_cleans_everything(monkeypatch):
    """After DB sweep marks a call ended, reap_ended_calls must purge
    in-memory state AND release the SFU router."""
    from app.services import topology_manager as tm_module
    from app.services.call_service import CallService

    backend = _FakeBackend()
    tm_module.topology_manager = tm_module.TopologyManager(backend=backend)
    monkeypatch.setattr(
        tm_module.topology_manager, "_broadcast_switch", _noop_broadcast
    )

    svc = CallService()
    import app.services.call_state_persistence as persistence
    for name in (
        "upsert_call", "add_participant", "mark_active", "mark_ended",
        "remove_participant",
    ):
        if hasattr(persistence.call_state_persistence, name):
            async def _noop(*a, **k):
                return None
            monkeypatch.setattr(persistence.call_state_persistence, name, _noop)

    uid = uuid.uuid4().hex
    call = await svc.initiate_call(initiator_id=uid, call_type="video", routing="mesh")
    for extra in range(4):
        call.add_participant(f"u{extra}")
    await tm_module.topology_manager.force_switch(
        call, "sfu", reason="participant_count"
    )
    assert call.call_id in svc._active_calls
    assert call.call_id in backend.allocated

    # Simulate the sweep loop feeding a list of swept call_ids
    reaped = await svc.reap_ended_calls([call.call_id, "nonexistent-id"])
    assert reaped == 1
    assert call.call_id not in svc._active_calls
    assert uid not in svc._user_calls

    # Drain the fire-and-forget router release
    for _ in range(20):
        if call.call_id in backend.released:
            break
        await asyncio.sleep(0.01)
    assert call.call_id in backend.released


async def test_call_service_shutdown_drains_bg_tasks(monkeypatch):
    """shutdown() must await pending topology-release tasks."""
    from app.services import topology_manager as tm_module
    from app.services.call_service import CallService

    released: list[str] = []

    class _SlowBackend(SFUBackend):
        async def allocate_router(self, call_id: str):
            return {"backend": "fake", "url": f"rtc://fake/{call_id}", "producer_token": "t"}

        async def release_router(self, call_id: str) -> None:
            await asyncio.sleep(0.05)
            released.append(call_id)

    tm_module.topology_manager = tm_module.TopologyManager(backend=_SlowBackend())
    monkeypatch.setattr(
        tm_module.topology_manager, "_broadcast_switch", _noop_broadcast
    )

    svc = CallService()
    import app.services.call_state_persistence as persistence
    for name in (
        "upsert_call",
        "add_participant",
        "mark_active",
        "mark_ended",
        "remove_participant",
    ):
        if hasattr(persistence.call_state_persistence, name):
            async def _noop(*a, **k):
                return None
            monkeypatch.setattr(persistence.call_state_persistence, name, _noop)

    uid = uuid.uuid4().hex
    call = await svc.initiate_call(initiator_id=uid, call_type="audio", routing="mesh")
    for extra in range(4):
        call.add_participant(f"u{extra}")
    await tm_module.topology_manager.force_switch(
        call, "sfu", reason="participant_count"
    )

    await svc.hangup(call.call_id, uid)
    # shutdown must drain even though release_router sleeps
    await svc.shutdown()
    assert released == [call.call_id]


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────


async def _noop_broadcast(**kwargs: Any) -> None:
    return None
