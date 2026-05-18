"""
End-to-end test for the UDP multi-hop relay.

Spins up three `RelayManager` instances on the same event loop simulating
three Helen servers A → B → C, wires them into a chain, and verifies a
UDP payload sent at the entry port is delivered to a final destination
socket on the far side. Return-path traffic is also asserted.

No HMAC / HTTP layer here — that's exercised separately by
`test_federation_endpoints.py`. This test isolates the relay plumbing
itself so a network regression can be pinpointed without running a
multi-server integration harness.
"""

from __future__ import annotations

import asyncio
import socket

import pytest

from app.services.relay_worker import RelayManager


@pytest.mark.asyncio
async def test_two_hop_relay_forwards_and_returns():
    # ── Final destination: a blocking UDP socket (we use threads to
    #    drive it so the relay event loop isn't competing with the
    #    destination for the selector).
    dst_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    dst_sock.bind(("127.0.0.1", 0))
    dst_sock.settimeout(2.0)
    dst_host, dst_port = dst_sock.getsockname()

    # ── Two "servers", each with their own RelayManager ──
    mgr_b = RelayManager()
    mgr_c = RelayManager()
    await mgr_b.start(bind_host="127.0.0.1")
    await mgr_c.start(bind_host="127.0.0.1")

    try:
        # Allocate the far hop first so we know its ingress port.
        hop_c = await mgr_c.allocate(
            next_hop_host=dst_host, next_hop_port=dst_port, idle_ttl=30.0,
        )
        # Allocate the near hop pointing at the far hop.
        hop_b = await mgr_b.allocate(
            next_hop_host="127.0.0.1",
            next_hop_port=hop_c.ingress_port,
            idle_ttl=30.0,
        )

        # ── Client: blocking UDP socket, also driven from threads. ──
        client_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        client_sock.bind(("127.0.0.1", 0))
        client_sock.settimeout(2.0)

        # Kick the executor BEFORE sending so the thread is already parked
        # in recvfrom when the relay forwards the packet.
        loop = asyncio.get_event_loop()
        recv_future = loop.run_in_executor(None, dst_sock.recvfrom, 2048)
        await asyncio.sleep(0.05)

        # Client → entry
        client_sock.sendto(
            b"hello-relay", ("127.0.0.1", hop_b.ingress_port),
        )

        received, src_addr = await recv_future
        assert received == b"hello-relay", f"dst did not receive; got {received!r}"

        # Counters moved.
        assert hop_b.packets_forwarded >= 1
        assert hop_c.packets_forwarded >= 1
        assert hop_b.bytes_forwarded >= len(b"hello-relay")

        # ── Return path ──
        echo_future = loop.run_in_executor(None, client_sock.recvfrom, 2048)
        await asyncio.sleep(0.05)
        dst_sock.sendto(b"echo-back", src_addr)
        echoed, _from = await echo_future
        assert echoed == b"echo-back", f"client did not receive echo; got {echoed!r}"

        client_sock.close()
    finally:
        await mgr_b.stop()
        await mgr_c.stop()
        dst_sock.close()


@pytest.mark.asyncio
async def test_relay_session_release_closes_sockets():
    dst_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    dst_sock.setblocking(False)
    dst_sock.bind(("127.0.0.1", 0))
    dst_host, dst_port = dst_sock.getsockname()

    mgr = RelayManager()
    await mgr.start(bind_host="127.0.0.1")
    try:
        s = await mgr.allocate(dst_host, dst_port, idle_ttl=30.0)
        rid = s.relay_id
        assert mgr.get(rid) is not None

        released = await mgr.release(rid)
        assert released is True
        assert mgr.get(rid) is None

        # Re-release is a no-op.
        assert await mgr.release(rid) is False
    finally:
        await mgr.stop()
        dst_sock.close()


@pytest.mark.asyncio
async def test_relay_list_sessions_snapshot():
    mgr = RelayManager()
    await mgr.start(bind_host="127.0.0.1")
    try:
        dst = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        dst.bind(("127.0.0.1", 0))
        try:
            _host, port = dst.getsockname()
            await mgr.allocate("127.0.0.1", port, idle_ttl=30.0)
            await mgr.allocate("127.0.0.1", port, idle_ttl=30.0)

            sessions = mgr.list_sessions()
            assert len(sessions) == 2
            required = {
                "relay_id", "ingress_host", "ingress_port",
                "next_hop_host", "next_hop_port",
                "age_seconds", "idle_seconds",
                "bytes_forwarded", "packets_forwarded",
            }
            assert required.issubset(sessions[0].keys())
        finally:
            dst.close()
    finally:
        await mgr.stop()
