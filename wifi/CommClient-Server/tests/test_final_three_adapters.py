"""
Unit tests for the final three adapters added to fill the
LAN-only matrix: SSH tunnels, ZeroMQ, RabbitMQ.

Pattern matches tests/test_new_transport_adapters.py — each test
focuses on:

  * Module imports cleanly without the optional dep installed
  * Singletons + lifecycle helpers behave
  * Pure-Python helpers (parsers, dataclasses) work correctly
  * Optional deps fail with a clear, actionable error message
"""

from __future__ import annotations

import pytest


# ── SSH tunnel manager ─────────────────────────────────────────────


def test_ssh_tunnel_specs_parser():
    from app.services.ssh_tunnel_manager import parse_tunnel_specs
    csv = (
        "local:helen@10.0.0.5:22:13000:peer.lan:3000,"
        "reverse:helen@10.0.0.6:22:13443:localhost:3443,"
        "malformed-entry"
    )
    specs = parse_tunnel_specs(csv)
    assert len(specs) == 2  # malformed dropped
    assert specs[0].direction == "local"
    assert specs[0].user == "helen"
    assert specs[0].host == "10.0.0.5"
    assert specs[0].bind_port == 13000
    assert specs[0].dest_port == 3000
    assert specs[1].direction == "reverse"
    assert specs[1].dest_host == "localhost"


def test_ssh_tunnel_specs_empty():
    from app.services.ssh_tunnel_manager import parse_tunnel_specs
    assert parse_tunnel_specs("") == []
    assert parse_tunnel_specs("   ") == []


@pytest.mark.asyncio
async def test_ssh_tunnel_manager_lifecycle(monkeypatch):
    import app.services.ssh_tunnel_manager as mod
    monkeypatch.setattr(mod, "_INSTANCE", None)
    assert mod.get_ssh_tunnels() is None
    await mod.shutdown_ssh_tunnels()  # safe on no-op
    assert mod.get_ssh_tunnels() is None


def test_ssh_tunnel_state_dataclass():
    from app.services.ssh_tunnel_manager import TunnelSpec, TunnelState
    spec = TunnelSpec(direction="local", user="helen",
                       host="10.0.0.5", port=22,
                       bind_port=13000, dest_host="peer", dest_port=3000)
    st = TunnelState(spec=spec)
    assert st.status == "starting"
    assert st.bytes_in == 0
    assert st.bytes_out == 0


def test_ssh_tunnel_manager_constructible_without_paramiko():
    """Constructor must not import paramiko — only start_one() does."""
    from app.services.ssh_tunnel_manager import SSHTunnelManager
    m = SSHTunnelManager()
    assert m.stats() == {"tunnel_count": 0, "tunnels": []}


# ── ZeroMQ adapter ─────────────────────────────────────────────────


def test_zeromq_adapter_imports():
    from app.services.zeromq_adapter import (
        ZeroMQAdapter, ZeroMQNotInstalledError,
    )
    a = ZeroMQAdapter("tcp://0.0.0.0:5555", peer_urls=["tcp://10.0.0.6:5555"])
    assert a.bind_url == "tcp://0.0.0.0:5555"
    assert len(a.peer_urls) == 1
    stats = a.stats()
    assert stats["connected"] is False
    assert stats["peer_count"] == 1


@pytest.mark.asyncio
async def test_zeromq_adapter_lifecycle(monkeypatch):
    import app.services.zeromq_adapter as mod
    monkeypatch.setattr(mod, "_INSTANCE", None)
    assert mod.get_zeromq() is None
    await mod.shutdown_zeromq()
    assert mod.get_zeromq() is None


def test_zeromq_publish_without_connect_raises():
    from app.services.zeromq_adapter import ZeroMQAdapter
    a = ZeroMQAdapter("tcp://0.0.0.0:5555")
    import asyncio
    with pytest.raises(RuntimeError, match="not connected"):
        asyncio.get_event_loop().run_until_complete(
            a.publish("x", {"y": 1}),
        )


# ── RabbitMQ adapter ───────────────────────────────────────────────


def test_rabbitmq_adapter_imports():
    from app.services.rabbitmq_adapter import (
        RabbitMQAdapter, RabbitMQNotInstalledError,
    )
    a = RabbitMQAdapter("amqp://guest:guest@127.0.0.1:5672/")
    assert a.url == "amqp://guest:guest@127.0.0.1:5672/"
    assert a.exchange_name == "helen.events"  # default
    stats = a.stats()
    assert stats["connected"] is False
    assert stats["exchange"] == "helen.events"


def test_rabbitmq_adapter_custom_exchange():
    from app.services.rabbitmq_adapter import RabbitMQAdapter
    a = RabbitMQAdapter(
        "amqp://x:y@10.0.0.5:5672/",
        exchange_name="helen.federation",
    )
    assert a.exchange_name == "helen.federation"


def test_rabbitmq_url_redaction_in_stats():
    """url_prefix in stats must NOT leak the password."""
    from app.services.rabbitmq_adapter import RabbitMQAdapter
    a = RabbitMQAdapter("amqp://user:supersecret@10.0.0.5:5672/")
    stats = a.stats()
    assert "supersecret" not in stats["url_prefix"]
    assert "10.0.0.5" in stats["url_prefix"]


@pytest.mark.asyncio
async def test_rabbitmq_adapter_lifecycle(monkeypatch):
    import app.services.rabbitmq_adapter as mod
    monkeypatch.setattr(mod, "_INSTANCE", None)
    assert mod.get_rabbitmq() is None
    await mod.shutdown_rabbitmq()
    assert mod.get_rabbitmq() is None


# ── End-to-end: backends summary endpoint includes new ones ────────


@pytest.mark.asyncio
async def test_backends_summary_includes_all_adapters(client, admin_headers):
    """The /api/admin/transports/backends endpoint must report state
    for every adapter we ship — old + new."""
    r = await client.get(
        "/api/admin/transports/backends", headers=admin_headers,
    )
    assert r.status_code == 200
    body = r.json()
    assert "active" in body
    for key in ("nats", "mqtt", "zeromq", "rabbitmq",
                "grpc_federation", "wireguard", "ssh_tunnels"):
        assert key in body["active"], f"missing {key}"
    assert "broker_backend" in body
    assert "ssh_tunnels_enabled" in body


@pytest.mark.asyncio
async def test_individual_status_endpoints_exist(client, admin_headers):
    """Each adapter has a /status endpoint that reports configured=False
    when not active (rather than 404)."""
    for adapter in ("nats", "mqtt", "zeromq", "rabbitmq",
                    "grpc", "wireguard", "ssh"):
        r = await client.get(
            f"/api/admin/transports/{adapter}/status",
            headers=admin_headers,
        )
        assert r.status_code == 200, \
            f"/transports/{adapter}/status should return 200, got {r.status_code}"
        body = r.json()
        assert "configured" in body
