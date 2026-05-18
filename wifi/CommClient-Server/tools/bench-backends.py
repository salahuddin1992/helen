"""
bench-backends.py — fan-out throughput + latency benchmark for every
broker backend Helen supports.

For each backend that's reachable, we:

  1. Start a subscriber that records the time each message lands.
  2. Have a publisher emit ``--count`` messages as fast as it can.
  3. Wait until the subscriber has seen them all (or times out).
  4. Compute: total elapsed seconds, msgs/sec throughput, p50/p95/p99
     latency from publish to handler-call.

Output is a side-by-side table so an operator can pick the right
backend for their workload (low-latency RPC vs high-throughput pub/sub
vs durable queueing).

Usage
-----
    # Bench every reachable backend with 1000 messages each:
    python tools/bench-backends.py --count 1000

    # Bench just one backend at higher load:
    python tools/bench-backends.py --backend nats --count 50000 \\
        --nats-url nats://127.0.0.1:4222

    # Compare against a remote broker:
    python tools/bench-backends.py --backend mqtt \\
        --mqtt-host 10.0.0.5 --mqtt-port 1883 --count 10000

The benchmark is **read-only** — it doesn't touch Helen's runtime
state, just talks to the broker directly via the same adapter classes
production code uses.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Ensure ``app.*`` is importable when running this from ``tools/``.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@dataclass
class BenchResult:
    backend: str
    sent: int = 0
    received: int = 0
    elapsed_s: float = 0.0
    p50_ms: float = 0.0
    p95_ms: float = 0.0
    p99_ms: float = 0.0
    throughput_msgs_per_sec: float = 0.0
    error: Optional[str] = None


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * (pct / 100.0)
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


# ── Per-backend benchmark drivers ──────────────────────────────────


async def bench_nats(url: str, count: int, timeout_s: float) -> BenchResult:
    r = BenchResult(backend="NATS")
    try:
        from app.services.nats_adapter import NATSAdapter
    except Exception as exc:
        r.error = f"adapter import failed: {exc}"
        return r
    a = NATSAdapter(url)
    try:
        await a.connect()
    except Exception as exc:
        r.error = f"connect failed: {exc}"
        return r

    latencies: list[float] = []
    received_event = asyncio.Event()

    async def handler(payload):
        latencies.append((time.perf_counter() - payload["t"]) * 1000.0)
        if len(latencies) >= count:
            received_event.set()

    try:
        await a.subscribe("helen.bench", handler)
        await asyncio.sleep(0.2)  # subscriber warmup

        t0 = time.perf_counter()
        for i in range(count):
            await a.publish("helen.bench", {"i": i, "t": time.perf_counter()})
        try:
            await asyncio.wait_for(received_event.wait(), timeout=timeout_s)
        except asyncio.TimeoutError:
            pass
        elapsed = time.perf_counter() - t0

        r.sent = count
        r.received = len(latencies)
        r.elapsed_s = elapsed
        r.throughput_msgs_per_sec = r.received / elapsed if elapsed > 0 else 0
        r.p50_ms = _percentile(latencies, 50)
        r.p95_ms = _percentile(latencies, 95)
        r.p99_ms = _percentile(latencies, 99)
    except Exception as exc:
        r.error = f"bench loop failed: {exc}"
    finally:
        await a.close()
    return r


async def bench_mqtt(host: str, port: int, count: int,
                     timeout_s: float) -> BenchResult:
    r = BenchResult(backend="MQTT")
    try:
        from app.services.mqtt_adapter import MQTTAdapter
    except Exception as exc:
        r.error = f"adapter import failed: {exc}"
        return r
    a = MQTTAdapter(host=host, port=port, client_id="helen-bench")
    try:
        await a.connect()
    except Exception as exc:
        r.error = f"connect failed: {exc}"
        return r

    latencies: list[float] = []
    received_event = asyncio.Event()

    async def handler(payload):
        latencies.append((time.perf_counter() - payload["t"]) * 1000.0)
        if len(latencies) >= count:
            received_event.set()

    try:
        await a.subscribe("helen.bench", handler)
        await asyncio.sleep(0.4)

        t0 = time.perf_counter()
        for i in range(count):
            await a.publish("helen.bench", {"i": i, "t": time.perf_counter()})
        try:
            await asyncio.wait_for(received_event.wait(), timeout=timeout_s)
        except asyncio.TimeoutError:
            pass
        elapsed = time.perf_counter() - t0

        r.sent = count
        r.received = len(latencies)
        r.elapsed_s = elapsed
        r.throughput_msgs_per_sec = r.received / elapsed if elapsed > 0 else 0
        r.p50_ms = _percentile(latencies, 50)
        r.p95_ms = _percentile(latencies, 95)
        r.p99_ms = _percentile(latencies, 99)
    except Exception as exc:
        r.error = f"bench loop failed: {exc}"
    finally:
        await a.close()
    return r


async def bench_zeromq(bind: str, count: int,
                        timeout_s: float) -> BenchResult:
    r = BenchResult(backend="ZeroMQ")
    try:
        from app.services.zeromq_adapter import ZeroMQAdapter
    except Exception as exc:
        r.error = f"adapter import failed: {exc}"
        return r
    a = ZeroMQAdapter(bind_url=bind)
    try:
        await a.connect()
    except Exception as exc:
        r.error = f"connect failed: {exc}"
        return r

    latencies: list[float] = []
    received_event = asyncio.Event()

    async def handler(payload):
        latencies.append((time.perf_counter() - payload["t"]) * 1000.0)
        if len(latencies) >= count:
            received_event.set()

    try:
        await a.subscribe("helen.bench", handler)
        await asyncio.sleep(0.3)

        t0 = time.perf_counter()
        for i in range(count):
            await a.publish("helen.bench", {"i": i, "t": time.perf_counter()})
        try:
            await asyncio.wait_for(received_event.wait(), timeout=timeout_s)
        except asyncio.TimeoutError:
            pass
        elapsed = time.perf_counter() - t0

        r.sent = count
        r.received = len(latencies)
        r.elapsed_s = elapsed
        r.throughput_msgs_per_sec = r.received / elapsed if elapsed > 0 else 0
        r.p50_ms = _percentile(latencies, 50)
        r.p95_ms = _percentile(latencies, 95)
        r.p99_ms = _percentile(latencies, 99)
    except Exception as exc:
        r.error = f"bench loop failed: {exc}"
    finally:
        await a.close()
    return r


async def bench_rabbitmq(url: str, count: int,
                          timeout_s: float) -> BenchResult:
    r = BenchResult(backend="RabbitMQ")
    try:
        from app.services.rabbitmq_adapter import RabbitMQAdapter
    except Exception as exc:
        r.error = f"adapter import failed: {exc}"
        return r
    a = RabbitMQAdapter(url)
    try:
        await a.connect()
    except Exception as exc:
        r.error = f"connect failed: {exc}"
        return r

    latencies: list[float] = []
    received_event = asyncio.Event()

    async def handler(payload):
        latencies.append((time.perf_counter() - payload["t"]) * 1000.0)
        if len(latencies) >= count:
            received_event.set()

    try:
        await a.subscribe("helen.bench.#", handler)
        await asyncio.sleep(0.4)

        t0 = time.perf_counter()
        for i in range(count):
            await a.publish("helen.bench.x", {"i": i, "t": time.perf_counter()})
        try:
            await asyncio.wait_for(received_event.wait(), timeout=timeout_s)
        except asyncio.TimeoutError:
            pass
        elapsed = time.perf_counter() - t0

        r.sent = count
        r.received = len(latencies)
        r.elapsed_s = elapsed
        r.throughput_msgs_per_sec = r.received / elapsed if elapsed > 0 else 0
        r.p50_ms = _percentile(latencies, 50)
        r.p95_ms = _percentile(latencies, 95)
        r.p99_ms = _percentile(latencies, 99)
    except Exception as exc:
        r.error = f"bench loop failed: {exc}"
    finally:
        await a.close()
    return r


# ── Renderer ──────────────────────────────────────────────────────


def render_table(results: list[BenchResult]) -> str:
    rows = [
        f"{'Backend':<10}  {'Sent':>7}  {'Recv':>7}  "
        f"{'Elapsed':>9}  {'Msgs/sec':>10}  "
        f"{'p50 ms':>8}  {'p95 ms':>8}  {'p99 ms':>8}",
        "-" * 90,
    ]
    for r in results:
        if r.error:
            rows.append(f"{r.backend:<10}  ERROR: {r.error[:75]}")
            continue
        rows.append(
            f"{r.backend:<10}  {r.sent:>7}  {r.received:>7}  "
            f"{r.elapsed_s:>8.2f}s  {r.throughput_msgs_per_sec:>10.1f}  "
            f"{r.p50_ms:>8.2f}  {r.p95_ms:>8.2f}  {r.p99_ms:>8.2f}"
        )
    return "\n".join(rows)


# ── Driver ────────────────────────────────────────────────────────


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend",
                        choices=["all", "nats", "mqtt", "zeromq", "rabbitmq"],
                        default="all")
    parser.add_argument("--count", type=int, default=1000)
    parser.add_argument("--timeout", type=float, default=30.0)

    parser.add_argument("--nats-url", default="nats://127.0.0.1:4222")
    parser.add_argument("--mqtt-host", default="127.0.0.1")
    parser.add_argument("--mqtt-port", type=int, default=1883)
    parser.add_argument("--zeromq-bind", default="tcp://127.0.0.1:0")
    parser.add_argument("--rabbitmq-url",
                        default="amqp://guest:guest@127.0.0.1:5672/")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    selected = {args.backend}
    if "all" in selected:
        selected = {"nats", "mqtt", "zeromq", "rabbitmq"}

    results: list[BenchResult] = []

    if "nats" in selected:
        print(f"Benching NATS ({args.count} msgs)...")
        results.append(await bench_nats(args.nats_url, args.count, args.timeout))
    if "mqtt" in selected:
        print(f"Benching MQTT ({args.count} msgs)...")
        results.append(await bench_mqtt(args.mqtt_host, args.mqtt_port,
                                          args.count, args.timeout))
    if "zeromq" in selected:
        print(f"Benching ZeroMQ ({args.count} msgs)...")
        results.append(await bench_zeromq(args.zeromq_bind,
                                            args.count, args.timeout))
    if "rabbitmq" in selected:
        print(f"Benching RabbitMQ ({args.count} msgs)...")
        results.append(await bench_rabbitmq(args.rabbitmq_url,
                                              args.count, args.timeout))

    if args.json:
        print(json.dumps([r.__dict__ for r in results], indent=2))
    else:
        print()
        print(render_table(results))
        print()
        # Recommend best for each axis.
        ok = [r for r in results if not r.error and r.received > 0]
        if ok:
            best_thr = max(ok, key=lambda r: r.throughput_msgs_per_sec)
            best_p50 = min(ok, key=lambda r: r.p50_ms)
            print(f"Highest throughput: {best_thr.backend} "
                   f"({best_thr.throughput_msgs_per_sec:.0f} msgs/s)")
            print(f"Lowest p50 latency: {best_p50.backend} "
                   f"({best_p50.p50_ms:.2f} ms)")

    return 0 if all(r.received > 0 or r.error for r in results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
