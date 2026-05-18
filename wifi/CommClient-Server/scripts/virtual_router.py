"""
Virtual router -- minimal asyncio TCP proxy that simulates a real
router sitting between a Helen client and a Helen server.

Topology
--------
                                      +----------------------+
   client                             |   Virtual Router     |
   (127.0.0.1:9100)  ----- connect -->|  WAN: 0.0.0.0:9100   |
                                      |        | NAT/forward |
                                      |        v             |
                                      |  LAN: 127.0.0.5:3000 | ----> Helen-Server
                                      +----------------------+

The router accepts TCP on its WAN port and pumps bytes verbatim to a
configurable LAN endpoint. Every connection logs:
  * accept time + source port
  * total bytes in / out
  * close reason + duration

This is not a Layer-3 router — it's a userspace forwarder. But for
demonstrating "client talks to router which talks to server" without
needing Hyper-V / netns / wintun, it does the job: the client thinks
it's talking to ``127.0.0.1:9100`` and never sees the real server IP.

Usage
-----
    python scripts/virtual_router.py 9100 127.0.0.5 3000
                                     ^^^^ ^^^^^^^^^ ^^^^
                                     |    |        upstream port
                                     |    upstream host (the Helen server)
                                     listen port (router's WAN IP)
"""

from __future__ import annotations

import asyncio
import sys
import time
from typing import Tuple

# Connection counter — used in log lines so concurrent flows are easy to track.
_conn_counter = 0


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                conn_id: int, direction: str, stats: dict) -> None:
    """Copy bytes from reader to writer; tally into ``stats``."""
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
            stats[direction] = stats.get(direction, 0) + len(data)
    except (ConnectionError, OSError, asyncio.CancelledError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def handle_client(client_reader: asyncio.StreamReader,
                        client_writer: asyncio.StreamWriter,
                        upstream_host: str, upstream_port: int) -> None:
    global _conn_counter
    _conn_counter += 1
    cid = _conn_counter
    peer = client_writer.get_extra_info("peername") or ("?", 0)
    t0 = time.monotonic()
    stats: dict = {"in": 0, "out": 0}
    print(f"[router] #{cid} ACCEPT  client={peer[0]}:{peer[1]}", flush=True)

    try:
        upstream_reader, upstream_writer = await asyncio.open_connection(
            upstream_host, upstream_port,
        )
    except OSError as e:
        print(f"[router] #{cid} REJECT  upstream {upstream_host}:{upstream_port} unreachable ({e})",
              flush=True)
        client_writer.close()
        return

    print(f"[router] #{cid} FORWARD client {peer[0]}:{peer[1]} <-> "
          f"server {upstream_host}:{upstream_port}", flush=True)

    # Bidirectional pipes — "in" = client->server, "out" = server->client.
    await asyncio.gather(
        _pipe(client_reader, upstream_writer, cid, "in", stats),
        _pipe(upstream_reader, client_writer, cid, "out", stats),
        return_exceptions=True,
    )

    elapsed = time.monotonic() - t0
    print(f"[router] #{cid} CLOSE   in={stats['in']}B out={stats['out']}B "
          f"dur={elapsed*1000:.0f}ms", flush=True)


async def main() -> int:
    if len(sys.argv) != 4:
        print(__doc__)
        return 2
    listen_port = int(sys.argv[1])
    upstream_host = sys.argv[2]
    upstream_port = int(sys.argv[3])

    print(f"[router] starting: WAN 0.0.0.0:{listen_port} -> LAN "
          f"{upstream_host}:{upstream_port}")

    server = await asyncio.start_server(
        lambda r, w: handle_client(r, w, upstream_host, upstream_port),
        host="0.0.0.0", port=listen_port,
    )
    async with server:
        await server.serve_forever()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\n[router] shutting down")
