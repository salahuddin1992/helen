"""
The 100% bar — real-infrastructure end-to-end tests for every
LAN-only transport adapter.

This file proves each adapter works against either:
  * a real broker process spawned for the test, or
  * two separate Python processes communicating across the wire.

Tests skip cleanly if the infrastructure isn't available (no
mosquitto on PATH, no nats-server, no docker, no root, etc.) so the
file passes on a vanilla developer box and turns green segment by
segment as ops adds the brokers.
"""

from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

import pytest


# ── helpers ────────────────────────────────────────────────────────


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _wait_for_port(port: int, host: str = "127.0.0.1",
                   timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


# ── ZeroMQ multi-process ───────────────────────────────────────────


def _zmq_publisher_proc(bind_url: str, ready_q):
    import zmq
    ctx = zmq.Context.instance()
    pub = ctx.socket(zmq.PUB)
    pub.bind(bind_url)
    ready_q.put("ready")
    # Give subscriber time to connect — ZMQ has slow-joiner problem.
    time.sleep(0.8)
    for i in range(10):
        pub.send_multipart([b"helen.test.x",
                             json.dumps({"i": i, "from": "pub"}).encode()])
        time.sleep(0.05)
    pub.close()
    ctx.term()


def _zmq_subscriber_proc(connect_url: str, result_q):
    import zmq
    ctx = zmq.Context.instance()
    sub = ctx.socket(zmq.SUB)
    sub.setsockopt(zmq.SUBSCRIBE, b"helen.test.")
    sub.connect(connect_url)
    sub.setsockopt(zmq.RCVTIMEO, 4000)
    received = []
    try:
        for _ in range(5):
            try:
                parts = sub.recv_multipart()
                received.append(parts)
            except zmq.error.Again:
                break
    finally:
        sub.close()
        ctx.term()
    result_q.put(received)


def test_zeromq_two_separate_processes():
    pytest.importorskip("zmq")
    if os.name == "nt":
        pytest.skip("ZMQ multi-process on Windows + Python multiprocessing "
                    "+ proactor loop combo is racy — covered by in-process "
                    "round-trip elsewhere.")
    port = _free_port()
    url = f"tcp://127.0.0.1:{port}"
    ctx = multiprocessing.get_context("spawn")
    ready = ctx.Queue()
    result = ctx.Queue()
    pub = ctx.Process(target=_zmq_publisher_proc, args=(url, ready))
    sub = ctx.Process(target=_zmq_subscriber_proc, args=(url, result))
    sub.start()
    time.sleep(0.3)  # let SUB bind+connect first
    pub.start()
    pub.join(timeout=10)
    sub.join(timeout=10)
    received = result.get(timeout=2)
    assert len(received) >= 1, f"got {len(received)} messages"
    # Each message is [subject, json_body]
    body = json.loads(received[0][1].decode())
    assert body["from"] == "pub"


# ── gRPC cross-server (two server instances) ───────────────────────


@pytest.mark.asyncio
async def test_grpc_two_servers_cross_communication():
    """Server A and Server B both run the federation service; A's
    client sends an envelope to B; B's handler records it. Proves
    the same exact code path two real Helen-Servers would use."""
    pytest.importorskip("grpc")
    pytest.importorskip("grpc_tools")
    from app.services.grpc_federation import (
        GRPCFederationServer, GRPCFederationClient,
    )

    received_a, received_b = [], []

    async def handler_a(env): received_a.append(env); return {"error": ""}
    async def handler_b(env): received_b.append(env); return {"error": ""}

    port_a = _free_port()
    port_b = _free_port()
    srv_a = GRPCFederationServer(
        bind_host="127.0.0.1", bind_port=port_a,
        envelope_handler=handler_a,
    )
    srv_b = GRPCFederationServer(
        bind_host="127.0.0.1", bind_port=port_b,
        envelope_handler=handler_b,
    )
    await srv_a.start()
    await srv_b.start()

    # A's client → B's server
    client_a_to_b = GRPCFederationClient(endpoint=f"127.0.0.1:{port_b}")
    await client_a_to_b.connect()
    ack = await client_a_to_b.send_envelope({
        "event_id": "from-A",
        "event_type": "fed.message",
        "source_server_id": "server-A",
        "destination_server_id": "server-B",
        "payload": {"text": "hi from A"},
    })
    assert ack["success"]
    assert ack["event_id"] == "from-A"

    # B's client → A's server
    client_b_to_a = GRPCFederationClient(endpoint=f"127.0.0.1:{port_a}")
    await client_b_to_a.connect()
    ack2 = await client_b_to_a.send_envelope({
        "event_id": "from-B",
        "event_type": "fed.message",
        "source_server_id": "server-B",
        "destination_server_id": "server-A",
        "payload": {"text": "hi from B"},
    })
    assert ack2["success"]

    # Each server got exactly the message destined for it.
    assert len(received_a) == 1
    assert received_a[0]["source_server_id"] == "server-B"
    assert received_a[0]["payload"]["text"] == "hi from B"
    assert len(received_b) == 1
    assert received_b[0]["source_server_id"] == "server-A"
    assert received_b[0]["payload"]["text"] == "hi from A"

    await client_a_to_b.close()
    await client_b_to_a.close()
    await srv_a.stop()
    await srv_b.stop()


# ── NATS real broker ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_nats_real_broker_round_trip():
    pytest.importorskip("nats")
    nats_bin = shutil.which("nats-server") or shutil.which("nats-server.exe")
    if not nats_bin:
        pytest.skip("nats-server not on PATH; install via "
                    "`winget install Nats.Nats-Server` or download "
                    "from https://github.com/nats-io/nats-server/releases")
    port = _free_port()
    proc = subprocess.Popen(
        [nats_bin, "-a", "127.0.0.1", "-p", str(port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        if not _wait_for_port(port, timeout=8):
            pytest.fail("nats-server didn't start within 8s")
        from app.services.nats_adapter import NATSAdapter
        a = NATSAdapter(f"nats://127.0.0.1:{port}")
        await a.connect()

        received = []
        done = asyncio.Event()

        async def handler(payload):
            received.append(payload)
            done.set()

        await a.subscribe("helen.real.subject", handler)
        await asyncio.sleep(0.2)
        await a.publish("helen.real.subject",
                        {"hello": "real-nats", "n": 42})
        await asyncio.wait_for(done.wait(), timeout=4.0)
        assert received[0] == {"hello": "real-nats", "n": 42}
        await a.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()


# ── MQTT real broker (Mosquitto) ──────────────────────────────────


@pytest.mark.asyncio
async def test_mqtt_real_mosquitto_round_trip():
    pytest.importorskip("paho.mqtt")
    mosq = shutil.which("mosquitto") or shutil.which("mosquitto.exe")
    if not mosq:
        pytest.skip("mosquitto not on PATH; install via "
                    "`winget install EclipseFoundation.Mosquitto`")
    port = _free_port()
    # Minimal mosquitto config — anonymous + bind to localhost.
    with tempfile.NamedTemporaryFile(
        "w", suffix=".conf", delete=False,
    ) as cf:
        cf.write(f"listener {port} 127.0.0.1\nallow_anonymous true\n")
        cfg = cf.name
    proc = subprocess.Popen(
        [mosq, "-c", cfg, "-v"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        if not _wait_for_port(port, timeout=8):
            pytest.fail("mosquitto didn't start within 8s")

        from app.services.mqtt_adapter import MQTTAdapter
        a = MQTTAdapter(host="127.0.0.1", port=port,
                         client_id="helen-test-100")
        await a.connect()

        received = []
        done = asyncio.Event()

        async def handler(payload):
            received.append(payload)
            done.set()

        await a.subscribe("helen.real.mqtt", handler)
        await asyncio.sleep(0.5)
        await a.publish("helen.real.mqtt",
                        {"hello": "real-mqtt", "via": "mosquitto"})
        await asyncio.wait_for(done.wait(), timeout=5.0)
        assert received[0]["hello"] == "real-mqtt"
        await a.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
        os.unlink(cfg)


# ── RabbitMQ real broker (via docker, optional) ───────────────────


@pytest.mark.asyncio
async def test_rabbitmq_via_docker_round_trip():
    pytest.importorskip("aio_pika")
    if not shutil.which("docker"):
        pytest.skip("docker not on PATH")
    # Quick docker availability check
    try:
        rc = subprocess.run(
            ["docker", "info"], capture_output=True, timeout=4,
        ).returncode
    except subprocess.TimeoutExpired:
        rc = 1
    if rc != 0:
        pytest.skip("docker daemon not running")

    port = _free_port()
    container = subprocess.Popen(
        ["docker", "run", "--rm", "-p", f"{port}:5672",
         "--name", f"helen-rabbit-test-{port}",
         "rabbitmq:3-alpine"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        # RabbitMQ + Erlang takes ~25-40s to be ready.
        if not _wait_for_port(port, timeout=60):
            pytest.skip("RabbitMQ container didn't bind in 60s")
        # Even after port is open, AMQP handshake needs a bit more.
        await asyncio.sleep(8)

        from app.services.rabbitmq_adapter import RabbitMQAdapter
        a = RabbitMQAdapter(
            f"amqp://guest:guest@127.0.0.1:{port}/",
        )
        await a.connect()

        received = []
        done = asyncio.Event()

        async def handler(payload):
            received.append(payload)
            done.set()

        await a.subscribe("helen.test.#", handler)
        await asyncio.sleep(0.5)
        await a.publish("helen.test.real",
                        {"hello": "rabbitmq", "via": "docker"})
        await asyncio.wait_for(done.wait(), timeout=10.0)
        assert received[0]["hello"] == "rabbitmq"
        await a.close()
    finally:
        subprocess.run(
            ["docker", "stop", f"helen-rabbit-test-{port}"],
            capture_output=True, timeout=10,
        )
        container.wait(timeout=5)


# ── Direct P2P with simulated NAT (UDP hole punching primitive) ───


def test_direct_p2p_udp_with_nat_like_routing():
    """Simulate the fundamental hole-punching primitive: A learns
    B's external endpoint via a 'rendezvous' (third process), then
    A and B speak directly. We use 3 sockets locally — A, B, and
    a 'rendezvous' — to model the path."""
    sock_a = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock_a.bind(("127.0.0.1", 0))
    sock_b = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock_b.bind(("127.0.0.1", 0))
    rdv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rdv.bind(("127.0.0.1", 0))
    rdv_addr = rdv.getsockname()

    # 1. A registers with rendezvous (typical STUN-style)
    sock_a.sendto(b"register:A", rdv_addr)
    msg, a_ext = rdv.recvfrom(1024)
    assert msg == b"register:A"

    # 2. B registers
    sock_b.sendto(b"register:B", rdv_addr)
    msg, b_ext = rdv.recvfrom(1024)
    assert msg == b"register:B"

    # 3. Rendezvous tells A about B's external endpoint
    rdv.sendto(f"peer:{b_ext[0]}:{b_ext[1]}".encode(),
               a_ext)
    info, _ = sock_a.recvfrom(1024)
    parts = info.decode().split(":")
    peer_host, peer_port = parts[1], int(parts[2])

    # 4. A "punches" by sending to B's external endpoint
    sock_a.sendto(b"hello-from-A", (peer_host, peer_port))
    received_at_b, src = sock_b.recvfrom(1024)
    assert received_at_b == b"hello-from-A"

    # 5. B replies, completing the bidirectional path
    sock_b.sendto(b"hello-from-B", src)
    reply, _ = sock_a.recvfrom(1024)
    assert reply == b"hello-from-B"

    sock_a.close(); sock_b.close(); rdv.close()
