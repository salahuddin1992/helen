"""
Load-test harness for the UDP multi-hop relay.

Spins up an in-process echo destination and a configurable number of
parallel `RelayManager` chains (each 1–N hops deep), then pumps UDP
traffic through every chain for `DURATION_S` seconds. Measures:

  * Throughput — packets/sec and bytes/sec sent through the entry port.
  * Echo round-trip — (send, echo-receive) latency percentiles.
  * Loss — packets sent minus packets echoed.

This exercises the relay plumbing and the supervisor without the HTTP
layer, so a regression in packet handling shows up as loss/latency in
this harness before it shows up in a full multi-server deployment.

Run:
    python scripts/load_test_relay.py --chains 50 --hops 2 --duration 10 \
                                      --packet-size 600 --rate 200

`--rate` is packets-per-second PER CHAIN; `--chains 50 --rate 200` means
10 000 PPS aggregate through the local relay plant.
"""

from __future__ import annotations

import argparse
import asyncio
import socket
import statistics
import struct
import sys
import time
from pathlib import Path

# Allow running from the repo root without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.relay_worker import RelayManager  # noqa: E402


async def _echo_server(stop_evt: asyncio.Event) -> tuple[str, int, asyncio.Task]:
    """Blocking UDP echo pushed onto a dedicated thread so it doesn't
    fight the relay event loop for selector wakeups."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(0.2)
    host, port = sock.getsockname()

    def run():
        while not stop_evt.is_set():
            try:
                data, src = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                sock.sendto(data, src)
            except OSError:
                pass
        sock.close()

    task = asyncio.get_event_loop().run_in_executor(None, run)
    return host, port, task


async def _run_one_chain(
    mgrs: list[RelayManager],
    echo_host: str,
    echo_port: int,
    duration: float,
    rate: int,
    packet_size: int,
    stats: dict,
) -> None:
    """Allocate a chain through every mgr in order and drive traffic."""
    # Reverse order: the last hop is programmed with the echo server,
    # upstream hops with the next hop's ingress port.
    chain = []
    next_hop = (echo_host, echo_port)
    for m in reversed(mgrs):
        s = await m.allocate(next_hop[0], next_hop[1], idle_ttl=max(30.0, duration * 3))
        chain.append(s)
        next_hop = (s.ingress_host, s.ingress_port)
    entry_host, entry_port = next_hop

    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client.setblocking(False)
    client.bind(("127.0.0.1", 0))

    loop = asyncio.get_event_loop()
    seq = 0
    sent = 0
    received = 0
    latencies: list[float] = []
    pending: dict[int, float] = {}
    payload_tail = b"x" * max(0, packet_size - 16)

    async def _rx():
        nonlocal received
        while True:
            try:
                data, _ = await loop.sock_recvfrom(client, 4096)
            except (asyncio.CancelledError, OSError):
                return
            if len(data) < 16:
                continue
            s_seq, s_ts_ns = struct.unpack("!Qq", data[:16])
            start = pending.pop(s_seq, None)
            if start is not None:
                latencies.append((time.perf_counter_ns() - s_ts_ns) / 1_000_000)
                received += 1

    rx_task = asyncio.create_task(_rx())
    interval = 1.0 / rate if rate > 0 else 0
    start = time.perf_counter()
    end = start + duration

    while time.perf_counter() < end:
        seq += 1
        ts = time.perf_counter_ns()
        pkt = struct.pack("!Qq", seq, ts) + payload_tail
        pending[seq] = ts
        try:
            await loop.sock_sendto(client, pkt, (entry_host, entry_port))
            sent += 1
        except OSError:
            pass
        if interval:
            await asyncio.sleep(interval)

    # Drain — wait up to 1s for stragglers.
    await asyncio.sleep(1.0)
    rx_task.cancel()
    client.close()

    stats["sent"] += sent
    stats["received"] += received
    stats["latencies"].extend(latencies)


async def main(args) -> None:
    # Build `hops` RelayManagers (each simulates a separate server).
    mgrs = [RelayManager() for _ in range(args.hops)]
    for m in mgrs:
        await m.start(bind_host="127.0.0.1")

    stop_evt = asyncio.Event()
    echo_host, echo_port, echo_task = await _echo_server(stop_evt)

    stats = {"sent": 0, "received": 0, "latencies": []}
    try:
        await asyncio.gather(*[
            _run_one_chain(
                mgrs, echo_host, echo_port, args.duration,
                args.rate, args.packet_size, stats,
            )
            for _ in range(args.chains)
        ])
    finally:
        stop_evt.set()
        for m in mgrs:
            await m.stop()
        await echo_task

    # Report
    sent = stats["sent"]
    recv = stats["received"]
    loss = sent - recv
    lats = sorted(stats["latencies"]) or [0.0]
    pct = lambda p: lats[min(len(lats) - 1, int(len(lats) * p))]
    print("=== Relay load test ===")
    print(f"Chains:         {args.chains}  (hops each: {args.hops})")
    print(f"Duration:       {args.duration}s")
    print(f"Rate:           {args.rate} pps/chain  "
          f"(aggregate target: {args.chains * args.rate} pps)")
    print(f"Packet size:    {args.packet_size} bytes")
    print("")
    print(f"Sent:           {sent:>10}")
    print(f"Received:       {recv:>10}")
    print(f"Lost:           {loss:>10}  ({(loss / max(1, sent)) * 100:.2f}%)")
    print(f"Throughput:     {recv / args.duration:>10.0f} pps echoed")
    print(f"Latency p50:    {pct(0.50):>10.2f} ms")
    print(f"Latency p95:    {pct(0.95):>10.2f} ms")
    print(f"Latency p99:    {pct(0.99):>10.2f} ms")
    print(f"Latency max:    {max(lats):>10.2f} ms")
    if recv:
        print(f"Latency mean:   {statistics.fmean(lats):>10.2f} ms")


def _parse():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--chains", type=int, default=20,
                   help="parallel relay chains (default 20)")
    p.add_argument("--hops", type=int, default=2,
                   help="relay managers per chain (default 2)")
    p.add_argument("--duration", type=float, default=5.0,
                   help="seconds to run (default 5)")
    p.add_argument("--rate", type=int, default=100,
                   help="packets/sec/chain (default 100)")
    p.add_argument("--packet-size", type=int, default=300,
                   help="UDP payload bytes (default 300)")
    return p.parse_args()


if __name__ == "__main__":
    asyncio.run(main(_parse()))
