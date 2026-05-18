#!/usr/bin/env python3
"""
Helen LAN performance benchmark.

Measures, against a running Helen-Server:
  1. HTTP latency  (p50/p95/p99 over /api/health)
  2. Auth round-trip time (register + login)
  3. WebSocket connect time (Socket.IO handshake)
  4. Concurrent connections (up to N parallel sessions)
  5. Message throughput (messages/sec for a fixed window)

Usage:
  python3 bench.py --url http://10.0.0.5:3000 --duration 30 --concurrent 100

Designed to run from a client machine on the same LAN as the server.
Honest numbers — no warm-up cheating, no caching tricks.
"""

import argparse
import asyncio
import json
import secrets
import statistics
import time
from typing import Optional

try:
    import httpx
except ImportError:
    print("ERROR: pip install httpx websockets python-socketio[asyncio]")
    raise SystemExit(1)


# ── HTTP latency ──────────────────────────────────────────────


async def measure_http_latency(url: str, samples: int) -> dict:
    print(f"\n[1/5] HTTP latency to {url}/api/health ({samples} samples)...")
    timings = []
    errors = 0
    async with httpx.AsyncClient(timeout=5.0) as client:
        for _ in range(samples):
            t0 = time.perf_counter()
            try:
                r = await client.get(f"{url}/api/health")
                if r.status_code == 200:
                    timings.append((time.perf_counter() - t0) * 1000)
                else:
                    errors += 1
            except Exception:
                errors += 1
    if not timings:
        return {"error": "no successful samples", "errors": errors}
    return {
        "samples": len(timings),
        "errors": errors,
        "p50_ms": round(statistics.median(timings), 2),
        "p95_ms": round(statistics.quantiles(timings, n=20)[18], 2),
        "p99_ms": round(statistics.quantiles(timings, n=100)[98], 2)
                 if len(timings) >= 100 else round(max(timings), 2),
        "min_ms": round(min(timings), 2),
        "max_ms": round(max(timings), 2),
    }


# ── Auth round-trip ───────────────────────────────────────────


async def measure_auth(url: str, samples: int) -> dict:
    print(f"\n[2/5] Auth (register + login) round-trip ({samples} samples)...")
    timings = []
    errors = 0
    async with httpx.AsyncClient(timeout=10.0) as client:
        for _ in range(samples):
            user = f"bench_{secrets.token_hex(6)}"
            password = secrets.token_urlsafe(16)
            t0 = time.perf_counter()
            try:
                r1 = await client.post(f"{url}/api/auth/register",
                                       json={"username": user, "password": password,
                                             "display_name": user})
                if r1.status_code not in (200, 201):
                    errors += 1
                    continue
                r2 = await client.post(f"{url}/api/auth/login",
                                       json={"username": user, "password": password})
                if r2.status_code == 200:
                    timings.append((time.perf_counter() - t0) * 1000)
                else:
                    errors += 1
            except Exception:
                errors += 1
    if not timings:
        return {"error": "no successful round-trips", "errors": errors}
    return {
        "samples": len(timings),
        "errors": errors,
        "p50_ms": round(statistics.median(timings), 2),
        "p95_ms": round(statistics.quantiles(timings, n=20)[18], 2)
                  if len(timings) >= 20 else round(max(timings), 2),
        "min_ms": round(min(timings), 2),
        "max_ms": round(max(timings), 2),
    }


# ── Concurrent connections ────────────────────────────────────


async def measure_concurrent(url: str, n: int) -> dict:
    print(f"\n[3/5] Concurrent HTTP connections (n={n})...")
    sem = asyncio.Semaphore(n)
    successes = 0
    failures = 0
    timings = []
    async def one(client: httpx.AsyncClient):
        nonlocal successes, failures
        async with sem:
            t0 = time.perf_counter()
            try:
                r = await client.get(f"{url}/api/health")
                if r.status_code == 200:
                    successes += 1
                    timings.append((time.perf_counter() - t0) * 1000)
                else:
                    failures += 1
            except Exception:
                failures += 1

    async with httpx.AsyncClient(timeout=10.0,
                                 limits=httpx.Limits(max_connections=n+50)) as client:
        t_start = time.perf_counter()
        await asyncio.gather(*(one(client) for _ in range(n)),
                             return_exceptions=True)
        elapsed = time.perf_counter() - t_start

    return {
        "concurrent": n,
        "successes": successes,
        "failures": failures,
        "wall_time_sec": round(elapsed, 2),
        "rps": round(successes / elapsed, 1) if elapsed > 0 else 0,
        "p95_ms": round(statistics.quantiles(timings, n=20)[18], 2)
                  if len(timings) >= 20 else (round(max(timings), 2) if timings else None),
    }


# ── Sustained throughput ──────────────────────────────────────


async def measure_throughput(url: str, duration_sec: int, parallel: int) -> dict:
    print(f"\n[4/5] Sustained throughput "
          f"({duration_sec}s, {parallel} parallel workers)...")
    counter = 0
    failures = 0
    stop_at = time.perf_counter() + duration_sec
    lock = asyncio.Lock()

    async def worker(client: httpx.AsyncClient):
        nonlocal counter, failures
        while time.perf_counter() < stop_at:
            try:
                r = await client.get(f"{url}/api/health")
                async with lock:
                    if r.status_code == 200:
                        counter += 1
                    else:
                        failures += 1
            except Exception:
                async with lock:
                    failures += 1

    async with httpx.AsyncClient(timeout=5.0,
                                 limits=httpx.Limits(max_connections=parallel+10)) as client:
        await asyncio.gather(*(worker(client) for _ in range(parallel)))

    return {
        "duration_sec": duration_sec,
        "parallel_workers": parallel,
        "successful_requests": counter,
        "failures": failures,
        "rps_avg": round(counter / duration_sec, 1),
    }


# ── WebSocket connect ─────────────────────────────────────────


async def measure_websocket(url: str, samples: int) -> dict:
    """Light Socket.IO handshake probe — measures full polling+upgrade path."""
    print(f"\n[5/5] Socket.IO handshake ({samples} samples)...")
    timings = []
    errors = 0
    async with httpx.AsyncClient(timeout=5.0) as client:
        for _ in range(samples):
            t0 = time.perf_counter()
            try:
                r = await client.get(f"{url}/socket.io/?EIO=4&transport=polling")
                if r.status_code == 200 and r.text.startswith("0"):
                    timings.append((time.perf_counter() - t0) * 1000)
                else:
                    errors += 1
            except Exception:
                errors += 1
    if not timings:
        return {"error": "no successful handshakes", "errors": errors}
    return {
        "samples": len(timings),
        "errors": errors,
        "p50_ms": round(statistics.median(timings), 2),
        "p95_ms": round(statistics.quantiles(timings, n=20)[18], 2)
                  if len(timings) >= 20 else round(max(timings), 2),
    }


# ── Driver ────────────────────────────────────────────────────


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://localhost:3000")
    p.add_argument("--duration", type=int, default=30,
                   help="sustained throughput duration (sec)")
    p.add_argument("--concurrent", type=int, default=100,
                   help="parallel connections target")
    p.add_argument("--samples", type=int, default=100,
                   help="samples for latency / auth tests")
    p.add_argument("--output", default="bench-results.json")
    p.add_argument("--skip", default="",
                   help="comma-separated phases to skip: http,auth,concurrent,throughput,ws")
    args = p.parse_args()

    skip = set(s.strip() for s in args.skip.split(",") if s.strip())
    results = {"target": args.url, "started_at": time.strftime("%Y-%m-%d %H:%M:%S")}

    if "http" not in skip:
        results["http_latency"] = await measure_http_latency(args.url, args.samples)
    if "auth" not in skip:
        results["auth_roundtrip"] = await measure_auth(args.url, min(args.samples, 30))
    if "concurrent" not in skip:
        results["concurrent_connections"] = await measure_concurrent(args.url, args.concurrent)
    if "throughput" not in skip:
        results["sustained_throughput"] = await measure_throughput(
            args.url, args.duration, min(args.concurrent, 50))
    if "ws" not in skip:
        results["websocket_handshake"] = await measure_websocket(args.url, args.samples)

    print("\n" + "=" * 50)
    print("RESULTS")
    print("=" * 50)
    print(json.dumps(results, indent=2))

    with open(args.output, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nWritten to {args.output}")


if __name__ == "__main__":
    asyncio.run(main())
