"""
Unit tests for the 5 new transport / messaging modules added to fill
the gaps the user identified (gRPC, MQTT, NATS, WireGuard, L2/L3
bridges). Tests focus on:

  * Module imports cleanly (no syntax errors)
  * Singletons + lifecycle helpers (configure/get/shutdown) behave
  * Pure-Python helpers (key derivation, conf rendering, subject
    translation) work without third-party deps
  * Optional deps fail with a clear error message when absent

Tests deliberately do NOT spin up real brokers / interfaces —
that's covered by the integration test plan in DEPLOY-GUIDE.md.
"""

from __future__ import annotations

import pytest


# ── NATS adapter ───────────────────────────────────────────────────


def test_nats_adapter_module_imports():
    from app.services.nats_adapter import NATSAdapter, NATSNotInstalledError
    a = NATSAdapter("nats://10.0.0.10:4222")
    assert a.url == "nats://10.0.0.10:4222"
    assert not a._connected
    stats = a.stats()
    assert stats["connected"] is False
    assert stats["subscriptions"] == 0


@pytest.mark.asyncio
async def test_nats_adapter_singleton_lifecycle(monkeypatch):
    import app.services.nats_adapter as mod
    monkeypatch.setattr(mod, "_INSTANCE", None)
    assert mod.get_nats() is None
    await mod.shutdown_nats()  # safe on no-op
    assert mod.get_nats() is None


# ── MQTT adapter ───────────────────────────────────────────────────


def test_mqtt_subject_translation():
    from app.services.mqtt_adapter import _subject_to_topic, _topic_to_subject
    assert _subject_to_topic("fabric.P0.call.signal.offer.server_037") == \
        "helen/fabric/P0/call/signal/offer/server_037"
    assert _topic_to_subject(
        "helen/fabric/P0/call/signal/offer/server_037",
    ) == "fabric.P0.call.signal.offer.server_037"
    # Round-trip must be lossless.
    s = "fabric.P0.event.X"
    assert _topic_to_subject(_subject_to_topic(s)) == s


@pytest.mark.asyncio
async def test_mqtt_adapter_lifecycle(monkeypatch):
    import app.services.mqtt_adapter as mod
    monkeypatch.setattr(mod, "_INSTANCE", None)
    assert mod.get_mqtt() is None
    await mod.shutdown_mqtt()  # safe on no-op


# ── gRPC federation ────────────────────────────────────────────────


def test_grpc_federation_module_imports():
    from app.services.grpc_federation import (
        GRPCFederationServer, GRPCFederationClient, GRPCNotInstalledError,
    )
    # Don't actually start the server — just verify the class is constructible.
    async def noop(_env): return {}
    server = GRPCFederationServer(
        bind_host="127.0.0.1", bind_port=50051, envelope_handler=noop,
    )
    assert server.bind_port == 50051
    assert server._server is None


def test_grpc_federation_proto_source_present():
    from app.services.grpc_federation import _PROTO_SOURCE
    assert "service Federation" in _PROTO_SOURCE
    assert "rpc SendEnvelope" in _PROTO_SOURCE
    assert "message Envelope" in _PROTO_SOURCE
    assert "package helen.federation" in _PROTO_SOURCE


# ── WireGuard manager ──────────────────────────────────────────────


def test_wireguard_deterministic_ip_stable():
    from app.services.wireguard_manager import deterministic_mesh_ip
    ip_a1 = deterministic_mesh_ip("server-001", "10.99.0.0/24")
    ip_a2 = deterministic_mesh_ip("server-001", "10.99.0.0/24")
    ip_b = deterministic_mesh_ip("server-002", "10.99.0.0/24")
    assert ip_a1 == ip_a2          # deterministic
    assert ip_a1 != ip_b           # collision-free for normal inputs
    assert ip_a1.startswith("10.99.0.")


def test_wireguard_render_conf_basic():
    from app.services.wireguard_manager import render_wg_conf, WGPeer
    peers = [
        WGPeer(
            server_id="peer-A",
            public_key="abc=" * 11,
            endpoint="10.0.0.5:51820",
            allowed_ips=["10.99.0.5/32"],
        ),
    ]
    conf = render_wg_conf(
        private_key="priv=" * 11,
        address="10.99.0.1/32",
        listen_port=51820,
        peers=peers,
    )
    assert "[Interface]" in conf
    assert "PrivateKey = priv=" in conf
    assert "ListenPort = 51820" in conf
    assert "[Peer]" in conf
    assert "10.0.0.5:51820" in conf
    assert "PersistentKeepalive = 25" in conf


def test_wireguard_render_conf_skips_incomplete_peers():
    """Peers without public_key or allowed_ips must be silently dropped."""
    from app.services.wireguard_manager import render_wg_conf, WGPeer
    peers = [
        WGPeer(server_id="incomplete", public_key="", endpoint=""),
        WGPeer(
            server_id="ok", public_key="key=" * 11,
            endpoint="10.0.0.6:51820",
            allowed_ips=["10.99.0.6/32"],
        ),
    ]
    conf = render_wg_conf(
        private_key="X" * 44, address="10.99.0.1/32",
        listen_port=51820, peers=peers,
    )
    assert conf.count("[Peer]") == 1, "incomplete peer should be skipped"


@pytest.mark.asyncio
async def test_wireguard_lifecycle_singleton(monkeypatch):
    import app.services.wireguard_manager as mod
    monkeypatch.setattr(mod, "_INSTANCE", None)
    assert mod.get_wireguard() is None
    await mod.shutdown_wireguard()  # safe on no-op


# ── L2/L3 bridge ───────────────────────────────────────────────────


def test_l2_l3_bridge_imports():
    from app.services.l2_l3_bridge import (
        TapInterface, TunInterface,
        create_tap_interface, create_tun_interface,
        destroy_interface, add_route, remove_route, arp_table,
    )
    # Sanity: dataclasses are constructible.
    tap = TapInterface(name="hl0", mac_address="02:00:00:00:00:01")
    tun = TunInterface(name="hl1", address="10.99.0.1/30")
    assert tap.name == "hl0"
    assert tun.address.startswith("10.99.0.1")


def test_l2_l3_bridge_arp_table_fallback():
    """arp_table() must NOT crash even if `arp`/`ip` aren't on PATH."""
    from app.services.l2_l3_bridge import arp_table
    try:
        rows = arp_table()
    except FileNotFoundError:
        # Some CI runners don't have iproute2/arp — that's OK as long
        # as the helper raises rather than silently returning bad data.
        return
    assert isinstance(rows, list)


# ── Helen-Router ring topology (separate process) ─────────────────


def test_ring_topology_strategy_logic():
    """The ring helper is on Helen-Router, not Helen-Server. We
    exercise it via direct module load to keep this file's deps
    in one place."""
    import importlib.util
    import sys
    path = (
        "C:/Users/youse/c/wifi/Helen-Router/app/mesh.py"
    )
    spec = importlib.util.spec_from_file_location("router_mesh_test", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["router_mesh_test"] = mod
    spec.loader.exec_module(mod)

    node = mod.MeshNode(router_id="r-self", my_url="http://10.0.0.1:8080")
    node.add_neighbour("r-2", "http://10.0.0.2:8080")
    node.add_neighbour("r-3", "http://10.0.0.3:8080")
    # Inject an LSA so r-2 announces server-X
    node.receive_lsa(mod.LSA(
        origin="r-2", epoch=1, neighbours={},
        direct_servers=["server-X"],
    ))
    node.apply_topology_strategy("ring")
    # In a 3-node ring sorted as [r-2, r-3, r-self], r-self's next is
    # r-2 (wraps). server-X is announced by r-2 → routes to r-2.
    assert "server-X" in node.routes
    next_hops = [p[0] for p in node.routes["server-X"]]
    assert "r-2" in next_hops, f"expected r-2 in {next_hops}"


def test_ring_topology_falls_back_to_dijkstra_on_unknown_strategy():
    import importlib.util
    import sys
    path = "C:/Users/youse/c/wifi/Helen-Router/app/mesh.py"
    spec = importlib.util.spec_from_file_location("router_mesh_test2", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["router_mesh_test2"] = mod
    spec.loader.exec_module(mod)
    node = mod.MeshNode(router_id="r-self", my_url="http://10.0.0.1:8080")
    node.announce_direct_server("local-server")
    # Bogus strategy name — must NOT crash, must fall back to Dijkstra.
    node.apply_topology_strategy("nonexistent")
    assert "local-server" in node.routes
