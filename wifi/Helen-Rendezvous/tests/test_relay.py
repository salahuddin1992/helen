"""Tests for the blind TCP relay (RelayHub).

The relay joins a parked backend (Helen-Server) socket to a frontend
(external client) socket. The regression these tests guard against: while a
backend was parked, `handle_backend` used to keep reading its own socket to
detect disconnects. Once a frontend claimed the pair, `_join_streams` also
read that same socket — two concurrent readers racing for the backend→client
byte stream, which silently dropped payload bytes. `test_relay_roundtrip`
pushes a payload larger than one read chunk in each direction and asserts it
arrives intact, so a reintroduction of the race fails here.
"""

import asyncio
import contextlib

import pytest

import main


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    # handle_backend refuses to register when no bootstrap token is configured.
    monkeypatch.setattr(main, "BOOTSTRAP_TOKEN", "test-token")


async def _start(hub):
    backend_srv = await asyncio.start_server(hub.handle_backend, "127.0.0.1", 0)
    frontend_srv = await asyncio.start_server(hub.handle_frontend, "127.0.0.1", 0)
    bport = backend_srv.sockets[0].getsockname()[1]
    fport = frontend_srv.sockets[0].getsockname()[1]
    return backend_srv, frontend_srv, bport, fport


async def _readline(reader):
    return (await asyncio.wait_for(reader.readline(), timeout=5.0)).strip()


async def test_relay_roundtrip():
    hub = main.RelayHub()
    backend_srv, frontend_srv, bport, fport = await _start(hub)
    conns = []
    try:
        # Backend registers and parks.
        br, bw = await asyncio.open_connection("127.0.0.1", bport)
        conns += [bw]
        bw.write(b"REGISTER pid123\n")
        await bw.drain()
        assert await _readline(br) == b"OK waiting"

        # Let the backend coroutine enqueue the registration.
        await asyncio.sleep(0.05)

        # Frontend looks the backend up and is joined to it.
        fr, fw = await asyncio.open_connection("127.0.0.1", fport)
        conns += [fw]
        fw.write(b"LOOKUP pid123\n")
        await fw.drain()
        assert await _readline(fr) == b"OK joined"
        assert await _readline(br) == b"GO"

        # Payload larger than the 64 KiB pipe chunk, both directions. A
        # concurrent reader on the backend socket would steal bytes here.
        up = b"u" * 200_000
        fw.write(up)
        await fw.drain()
        assert await asyncio.wait_for(br.readexactly(len(up)), timeout=5.0) == up

        down = b"d" * 200_000
        bw.write(down)
        await bw.drain()
        assert await asyncio.wait_for(fr.readexactly(len(down)), timeout=5.0) == down
    finally:
        for w in conns:
            with contextlib.suppress(Exception):
                w.close()
        backend_srv.close()
        frontend_srv.close()
        await backend_srv.wait_closed()
        await frontend_srv.wait_closed()


async def test_relay_lookup_without_backend():
    hub = main.RelayHub()
    backend_srv, frontend_srv, bport, fport = await _start(hub)
    try:
        fr, fw = await asyncio.open_connection("127.0.0.1", fport)
        fw.write(b"LOOKUP nobody\n")
        await fw.drain()
        assert await _readline(fr) == b"ERR no backend registered"
        fw.close()
    finally:
        backend_srv.close()
        frontend_srv.close()
        await backend_srv.wait_closed()
        await frontend_srv.wait_closed()


async def test_relay_backend_bad_greeting():
    hub = main.RelayHub()
    backend_srv, frontend_srv, bport, fport = await _start(hub)
    try:
        br, bw = await asyncio.open_connection("127.0.0.1", bport)
        bw.write(b"HELLO oops\n")
        await bw.drain()
        assert await _readline(br) == b"ERR bad greeting"
        bw.close()
    finally:
        backend_srv.close()
        frontend_srv.close()
        await backend_srv.wait_closed()
        await frontend_srv.wait_closed()
