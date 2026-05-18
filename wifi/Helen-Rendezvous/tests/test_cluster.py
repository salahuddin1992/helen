"""
Tests for cluster coordination primitives:
    * InstanceRegistry heartbeat + auto-deregister
    * SessionAffinity bind/lookup/release/refresh
    * CrossInstanceRelay end-to-end (two relays sharing one backend)
"""

from __future__ import annotations

import asyncio

import pytest


pytestmark = pytest.mark.asyncio


@pytest.fixture()
async def backend():
    from storage.memory_backend import MemoryBackend
    b = MemoryBackend(cleanup_interval=0.2)
    await b.start()
    yield b
    await b.close()


async def test_instance_registry_heartbeat(backend):
    from cluster.instance_registry import InstanceRegistry
    reg = InstanceRegistry(
        backend, port=9000, version="t",
        heartbeat_interval=0.2, heartbeat_ttl=2,
    )
    reg.set_load_provider(lambda: {"score": 0.42})
    await reg.start_heartbeat()
    try:
        roster = await reg.list_active_instances()
        assert len(roster) == 1
        assert roster[0]["instance_id"] == reg.instance_id
        assert roster[0]["load"] == {"score": 0.42}
    finally:
        await reg.stop_heartbeat()
    roster_after = await reg.list_active_instances()
    assert all(e["instance_id"] != reg.instance_id for e in roster_after)


async def test_instance_registry_two_instances(backend):
    from cluster.instance_registry import InstanceRegistry
    a = InstanceRegistry(backend, port=9001, heartbeat_interval=0.2, heartbeat_ttl=3)
    b = InstanceRegistry(backend, port=9002, heartbeat_interval=0.2, heartbeat_ttl=3)
    await a.start_heartbeat()
    await b.start_heartbeat()
    try:
        roster = await a.list_active_instances()
        ids = {x["instance_id"] for x in roster}
        assert a.instance_id in ids and b.instance_id in ids
    finally:
        await a.stop_heartbeat()
        await b.stop_heartbeat()


async def test_session_affinity(backend):
    from cluster.affinity import SessionAffinity
    aff = SessionAffinity(backend, ttl=10)
    assert await aff.bind("peer-A", "rdv-1", extra={"name": "ServerA"}) is True
    assert await aff.owner_of("peer-A") == "rdv-1"
    info = await aff.lookup("peer-A")
    assert info is not None and info["name"] == "ServerA"
    assert await aff.release("peer-A") is True
    assert await aff.owner_of("peer-A") is None


async def test_cross_instance_relay_request_response(backend):
    from cluster.cross_instance_relay import CrossInstanceRelay

    relay_a = CrossInstanceRelay(backend, "rdv-A")
    relay_b = CrossInstanceRelay(backend, "rdv-B")
    received_on_b: list[dict] = []

    async def b_handler(envelope: dict) -> None:
        received_on_b.append(envelope)
        await relay_b.respond(
            envelope["msg_id"], envelope["from_instance"],
            envelope["peer_id"],
            {"status": 200, "headers": [], "body_b64": "aGVsbG8="},
        )

    relay_b.on("tunnel_request", b_handler)
    await relay_a.start()
    await relay_b.start()
    await asyncio.sleep(0.1)  # allow subscribers to settle
    try:
        result = await relay_a.request(
            kind="tunnel_request",
            to_instance="rdv-B",
            peer_id="peer-99",
            payload={"method": "GET", "path": "/api/health"},
            timeout=2.0,
        )
        assert result["status"] == 200
        assert result["body_b64"] == "aGVsbG8="
        assert len(received_on_b) >= 1
        assert received_on_b[0]["peer_id"] == "peer-99"
    finally:
        await relay_a.stop()
        await relay_b.stop()


async def test_cross_instance_relay_ignores_self_broadcast(backend):
    from cluster.cross_instance_relay import CrossInstanceRelay
    relay = CrossInstanceRelay(backend, "solo")
    got: list[dict] = []
    relay.on("tunnel_request", lambda env: got.append(env) or asyncio.sleep(0))
    await relay.start()
    try:
        await relay.fire("tunnel_request", to_instance="solo", peer_id="x", payload={})
        await asyncio.sleep(0.2)
        # Self-loop messages must be filtered.
        assert not got
    finally:
        await relay.stop()


async def test_distributed_lock_via_memory(backend):
    tok = await backend.acquire_lock("foo", ttl=5)
    assert tok is not None
    assert await backend.acquire_lock("foo", ttl=5) is None
    assert await backend.release_lock("foo", tok) is True
    assert await backend.acquire_lock("foo", ttl=5) is not None
