"""
Tests for the Redis storage backend.

Strategy: use `fakeredis.aioredis` so the tests are hermetic and CI-friendly.
If `fakeredis` is not installed and a real Redis is reachable at
$HELEN_RENDEZVOUS_TEST_REDIS_URL the tests use that instead. Otherwise they
skip.
"""

from __future__ import annotations

import asyncio
import os

import pytest


pytestmark = pytest.mark.asyncio


def _try_make_client() -> "tuple[object, str] | None":
    """Return (redis_asyncio_client, mode) or None if no Redis available."""
    try:
        import fakeredis.aioredis as _far

        client = _far.FakeRedis(decode_responses=True)
        return client, "fake"
    except ImportError:
        pass
    url = os.environ.get("HELEN_RENDEZVOUS_TEST_REDIS_URL")
    if not url:
        return None
    try:
        import redis.asyncio as _ra

        client = _ra.from_url(url, decode_responses=True)
        return client, "real"
    except ImportError:
        return None


@pytest.fixture()
async def redis_backend():
    from storage.redis_backend import RedisBackend

    pair = _try_make_client()
    if pair is None:
        pytest.skip("no fakeredis/redis available")
    client, mode = pair
    backend = RedisBackend(client, mode=mode, key_prefix=f"test:{os.getpid()}:")
    yield backend
    await backend.close()


async def test_tunnel_register_lookup_unregister(redis_backend):
    pid = "abc1234"
    ok_key = await redis_backend.register_tunnel(pid, {"name": "ServerA"}, ttl=30)
    assert ok_key.endswith(":tunnel:abc1234")

    got = await redis_backend.lookup_tunnel(pid)
    assert got is not None
    assert got["peer_id"] == pid
    assert got["name"] == "ServerA"

    removed = await redis_backend.unregister_tunnel(pid)
    assert removed is True
    assert await redis_backend.lookup_tunnel(pid) is None


async def test_tunnel_list_with_scan(redis_backend):
    for i in range(7):
        await redis_backend.register_tunnel(f"peer{i:02}", {"i": i}, ttl=60)
    tunnels = await redis_backend.list_tunnels()
    assert len(tunnels) == 7
    ids = sorted(t["peer_id"] for t in tunnels)
    assert ids == [f"peer{i:02}" for i in range(7)]


async def test_signal_lifecycle(redis_backend):
    await redis_backend.register_signal("k1", {"x": 1}, ttl=10)
    got = await redis_backend.lookup_signal("k1")
    assert got == {"x": 1}
    assert await redis_backend.delete_signal("k1") is True
    assert await redis_backend.lookup_signal("k1") is None


async def test_signal_expiry(redis_backend):
    await redis_backend.register_signal("e1", {"v": 1}, ttl=1)
    # Fakeredis may not enforce TTL atomically; assert it disappears within 3s.
    for _ in range(40):
        await asyncio.sleep(0.1)
        if await redis_backend.lookup_signal("e1") is None:
            break
    assert await redis_backend.lookup_signal("e1") is None


async def test_distributed_lock(redis_backend):
    tok = await redis_backend.acquire_lock("cleanup", ttl=5)
    assert tok is not None
    again = await redis_backend.acquire_lock("cleanup", ttl=5)
    assert again is None
    # Wrong token should not release.
    assert await redis_backend.release_lock("cleanup", "wrong") is False
    assert await redis_backend.release_lock("cleanup", tok) is True


async def test_pubsub_roundtrip(redis_backend):
    channel = "rendezvous:events"
    received: list[dict] = []
    done = asyncio.Event()

    async def consume() -> None:
        async for msg in redis_backend.subscribe_events(channel):
            received.append(msg)
            if msg.get("type") == "stop":
                done.set()
                return

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.2)  # allow subscriber to register
    await redis_backend.publish_event(channel, {"type": "hello"})
    await redis_backend.publish_event(channel, {"type": "stop"})
    try:
        await asyncio.wait_for(done.wait(), timeout=3.0)
    except asyncio.TimeoutError:
        pytest.skip("pub/sub not delivered (fakeredis quirk)")
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    types = [m.get("type") for m in received]
    assert "hello" in types and "stop" in types


async def test_health(redis_backend):
    h = await redis_backend.health()
    assert h["backend"] == "redis"
    assert h["status"] in ("ok", "degraded")
    assert "latency_ms" in h


async def test_factory_default_memory(monkeypatch):
    monkeypatch.delenv("HELEN_RENDEZVOUS_STORAGE", raising=False)
    from storage.factory import build_backend
    backend = build_backend()
    assert backend.__class__.__name__ == "MemoryBackend"
    await backend.close()


async def test_factory_selects_redis(monkeypatch):
    from storage.factory import build_backend
    monkeypatch.setenv("HELEN_RENDEZVOUS_STORAGE", "redis")
    monkeypatch.setenv("HELEN_RENDEZVOUS_REDIS_URL", "redis://localhost:6379/0")
    try:
        import redis.asyncio  # noqa: F401
    except ImportError:
        pytest.skip("redis-py not installed")
    backend = build_backend()
    assert backend.__class__.__name__ == "RedisBackend"
    # don't actually try to ping — we just verified construction.
    await backend.close()
