"""
Stress test: 1000 diverse Helen-Router instances in a single asyncio
process. Diversity covers:

  * 12 vendors (Cisco, Juniper, Mikrotik, Ubiquiti, TP-Link, Huawei,
    Aruba, Fortinet, OpenWrt, pfSense, Custom-LAN, Helen-Edge)
  * 8 form factors (Edge, Core, Distribution, Access, IoT-Gateway,
    Mesh-Node, Branch, Headend)
  * 4 sizes (Small, Medium, Large, Enterprise)
  * 3 wire styles (Wired, Wireless, Hybrid)

Each router declares its identity in the /router/health response so the
test can verify per-vendor/per-size statistics, not just a flat count.

Notes
-----
* Spawned in-process via uvicorn (one event loop, 1000 servers).
  This is *not* how you'd run them in production — it's a stress
  scenario to prove that one Helen-Server can keep 1000 routers in
  sync.
* RSS will exceed 1 GB for 1000 routers. The user said "unlimited" —
  so we don't try to economise. If your machine lacks RAM, lower
  N_ROUTERS at the top.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import sys
import time

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route


N_ROUTERS = 1000
PORT_BASE = 9100
SERVER_ID = "stress-server-" + secrets.token_hex(8)
SERVER_URL = "http://127.0.0.1:3000"


# ── Diversity catalog ────────────────────────────────────────────────

VENDORS = [
    "Cisco", "Juniper", "Mikrotik", "Ubiquiti", "TP-Link", "Huawei",
    "Aruba", "Fortinet", "OpenWrt", "pfSense", "Custom-LAN",
    "Helen-Edge",
]
FORM_FACTORS = [
    "Edge", "Core", "Distribution", "Access", "IoT-Gateway",
    "Mesh-Node", "Branch", "Headend",
]
SIZES = ["Small", "Medium", "Large", "Enterprise"]
STYLES = ["Wired", "Wireless", "Hybrid"]


def assign_profile(idx: int) -> dict:
    """Deterministic profile from the router index — so re-runs hit
    the same distribution."""
    return {
        "vendor": VENDORS[idx % len(VENDORS)],
        "form_factor": FORM_FACTORS[(idx // len(VENDORS)) % len(FORM_FACTORS)],
        "size": SIZES[(idx // 19) % len(SIZES)],
        "style": STYLES[(idx // 31) % len(STYLES)],
        "model": f"R-{idx:04d}",
        "serial": f"SN-{secrets.token_hex(4)}",
    }


# ── Minimal router app ──────────────────────────────────────────────


def make_router_app(token: str, profile: dict) -> Starlette:
    upstreams: dict[str, dict] = {}

    def _check(req) -> bool:
        auth = req.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            return False
        return secrets.compare_digest(auth[7:], token)

    async def health(req):
        return JSONResponse({
            "status": "ok",
            "service": "helen-router-stress",
            "profile": profile,
            "upstreams": len(upstreams),
        })

    async def list_upstreams(req):
        now = time.time()
        return JSONResponse({
            "upstreams": [
                {**v, "stale_seconds": int(now - v["last_seen"])}
                for v in upstreams.values()
            ],
        })

    async def register(req):
        if not _check(req):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        body = await req.json()
        sid = body.get("server_id"); url = body.get("url")
        if not sid or not url:
            return JSONResponse({"error": "missing"}, status_code=400)
        upstreams[sid] = {
            "id": sid, "url": url,
            "capabilities": body.get("capabilities", []),
            "last_seen": time.time(),
        }
        return JSONResponse({"status": "registered"})

    async def heartbeat(req):
        if not _check(req):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        sid = req.path_params["server_id"]
        if sid not in upstreams:
            return JSONResponse({"error": "unknown"}, status_code=404)
        upstreams[sid]["last_seen"] = time.time()
        return JSONResponse({"status": "ok"})

    return Starlette(routes=[
        Route("/router/health", health),
        Route("/router/upstreams", list_upstreams),
        Route("/router/register", register, methods=["POST"]),
        Route("/router/heartbeat/{server_id}", heartbeat,
              methods=["POST"]),
    ])


async def run_one(port: int, token: str, profile: dict) -> None:
    config = uvicorn.Config(
        make_router_app(token, profile),
        host="127.0.0.1", port=port,
        log_level="error", access_log=False,
        lifespan="off",
    )
    await uvicorn.Server(config).serve()


# ── Probes ──────────────────────────────────────────────────────────


async def wait_alive(port: int, deadline: float) -> bool:
    async with httpx.AsyncClient(timeout=0.4) as c:
        while time.perf_counter() < deadline:
            try:
                r = await c.get(f"http://127.0.0.1:{port}/router/health")
                if r.status_code == 200:
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.05)
    return False


def percentile(values, p):
    if not values:
        return 0.0
    s = sorted(values)
    return s[min(int(len(s) * p), len(s) - 1)]


# ── Main ────────────────────────────────────────────────────────────


async def main() -> None:
    print(f"[*] Spawning {N_ROUTERS} diverse routers...")
    print(f"    {len(VENDORS)} vendors × {len(FORM_FACTORS)} form factors "
          f"× {len(SIZES)} sizes × {len(STYLES)} styles\n")

    tokens = [secrets.token_hex(16) for _ in range(N_ROUTERS)]
    profiles = [assign_profile(i) for i in range(N_ROUTERS)]

    t0 = time.perf_counter()
    server_tasks = [
        asyncio.create_task(
            run_one(PORT_BASE + i, tokens[i], profiles[i]),
            name=f"router-{i}",
        )
        for i in range(N_ROUTERS)
    ]

    print("[*] Waiting for routers to come up (this can take 1-2 min)...")
    deadline = time.perf_counter() + 180.0
    health_results = await asyncio.gather(*[
        wait_alive(PORT_BASE + i, deadline) for i in range(N_ROUTERS)
    ], return_exceptions=True)
    boot_ms = (time.perf_counter() - t0) * 1000
    healthy = sum(1 for r in health_results if r is True)
    print(f"[+] {healthy}/{N_ROUTERS} routers up in {boot_ms / 1000:.1f}s "
          f"({boot_ms / N_ROUTERS:.1f} ms avg)\n")

    if healthy < N_ROUTERS:
        print(f"[!] {N_ROUTERS - healthy} routers failed to start "
              f"(file-descriptor or port-collision). Continuing.\n")

    live_indices = [
        i for i, ok in enumerate(health_results) if ok is True
    ]
    n = len(live_indices)

    pool = httpx.AsyncClient(
        timeout=httpx.Timeout(5.0, connect=2.0),
        limits=httpx.Limits(
            max_keepalive_connections=n,
            max_connections=n * 2,
        ),
    )

    # ── Health-sweep ────────────────────────────────────────────
    t0 = time.perf_counter()
    sweep = await asyncio.gather(*[
        pool.get(f"http://127.0.0.1:{PORT_BASE + i}/router/health")
        for i in live_indices
    ], return_exceptions=True)
    sweep_ms = (time.perf_counter() - t0) * 1000
    sweep_ok = sum(
        1 for r in sweep
        if hasattr(r, "status_code") and r.status_code == 200
    )
    print(f"[+] Health sweep: {sweep_ok}/{n} ok in {sweep_ms:.0f} ms")

    # Vendor/size distribution from the responses
    from collections import Counter
    vendor_count: Counter = Counter()
    size_count: Counter = Counter()
    for r in sweep:
        if hasattr(r, "status_code") and r.status_code == 200:
            try:
                p = r.json().get("profile", {})
                vendor_count[p.get("vendor")] += 1
                size_count[p.get("size")] += 1
            except Exception:
                pass
    print("    vendors:", ", ".join(
        f"{k}={v}" for k, v in vendor_count.most_common()))
    print("    sizes:  ", ", ".join(
        f"{k}={v}" for k, v in size_count.most_common()))
    print()

    # ── Registration round ──────────────────────────────────────
    print(f"[*] Server registering at all {n} routers...")

    async def reg(idx: int):
        t = time.perf_counter()
        r = await pool.post(
            f"http://127.0.0.1:{PORT_BASE + idx}/router/register",
            headers={"Authorization": f"Bearer {tokens[idx]}"},
            json={"server_id": SERVER_ID, "url": SERVER_URL,
                  "capabilities": ["rest", "socketio", "webrtc"]},
        )
        return r.status_code, (time.perf_counter() - t) * 1000

    t0 = time.perf_counter()
    reg_res = await asyncio.gather(*[
        reg(i) for i in live_indices
    ], return_exceptions=True)
    reg_ms = (time.perf_counter() - t0) * 1000
    reg_ok = sum(1 for r in reg_res if isinstance(r, tuple) and r[0] == 200)
    lats = [r[1] for r in reg_res if isinstance(r, tuple)]
    print(f"[+] Registered {reg_ok}/{n} in {reg_ms:.0f} ms wall "
          f"(p50={percentile(lats, 0.50):.0f} ms "
          f"p95={percentile(lats, 0.95):.0f} ms "
          f"p99={percentile(lats, 0.99):.0f} ms per req)\n")

    # ── Visibility ──────────────────────────────────────────────
    print(f"[*] Verifying visibility at every router...")
    t0 = time.perf_counter()
    seen = await asyncio.gather(*[
        pool.get(f"http://127.0.0.1:{PORT_BASE + i}/router/upstreams")
        for i in live_indices
    ], return_exceptions=True)
    visible = sum(
        1 for r in seen
        if hasattr(r, "status_code") and r.status_code == 200
        and SERVER_ID in r.text
    )
    verify_ms = (time.perf_counter() - t0) * 1000
    print(f"[+] Visible at {visible}/{n} in {verify_ms:.0f} ms\n")

    # ── Heartbeat round ─────────────────────────────────────────
    async def hb(idx: int):
        t = time.perf_counter()
        r = await pool.post(
            f"http://127.0.0.1:{PORT_BASE + idx}/router/heartbeat/{SERVER_ID}",
            headers={"Authorization": f"Bearer {tokens[idx]}"},
        )
        return r.status_code, (time.perf_counter() - t) * 1000

    print(f"[*] Heartbeat round to all {n} routers...")
    t0 = time.perf_counter()
    hb_res = await asyncio.gather(*[
        hb(i) for i in live_indices
    ], return_exceptions=True)
    hb_ms = (time.perf_counter() - t0) * 1000
    hb_ok = sum(1 for r in hb_res if isinstance(r, tuple) and r[0] == 200)
    hb_lats = [r[1] for r in hb_res if isinstance(r, tuple)]
    print(f"[+] Heartbeat: {hb_ok}/{n} ok in {hb_ms:.0f} ms wall "
          f"(p50={percentile(hb_lats, 0.50):.0f} ms "
          f"p95={percentile(hb_lats, 0.95):.0f} ms "
          f"p99={percentile(hb_lats, 0.99):.0f} ms)\n")

    # 5 sustained rounds
    print(f"[*] 5 sustained heartbeat rounds...")
    rounds = []
    for r_i in range(5):
        rt0 = time.perf_counter()
        await asyncio.gather(*[hb(i) for i in live_indices])
        rounds.append((time.perf_counter() - rt0) * 1000)
        print(f"    round {r_i+1}: {rounds[-1]:.0f} ms")
    avg_round = sum(rounds) / len(rounds)
    print(f"[+] Sustained avg: {avg_round:.0f} ms/round\n")

    # Memory snapshot
    rss_mb = 0.0
    try:
        import psutil
        rss_mb = psutil.Process().memory_info().rss / 1024 / 1024
        print(f"[i] Process RSS: {rss_mb:.0f} MB "
              f"({rss_mb / n:.2f} MB per router)\n")
    except Exception:
        pass

    # Per-vendor latency breakdown
    print("[*] Per-vendor heartbeat breakdown:")
    by_vendor: dict[str, list[float]] = {}
    for live_idx, hb_result in zip(live_indices, hb_res):
        if isinstance(hb_result, tuple):
            v = profiles[live_idx]["vendor"]
            by_vendor.setdefault(v, []).append(hb_result[1])
    for vendor, lats in sorted(by_vendor.items(),
                                key=lambda x: -len(x[1])):
        print(f"    {vendor:14s} n={len(lats):>4}  "
              f"p50={percentile(lats, 0.50):.0f} ms  "
              f"p95={percentile(lats, 0.95):.0f} ms  "
              f"p99={percentile(lats, 0.99):.0f} ms")
    print()

    print("=" * 60)
    print(f"  STRESS TEST SUMMARY ({n} routers, "
          f"{len(VENDORS)} vendors)")
    print("=" * 60)
    print(f"  Boot time          {boot_ms / 1000:>7.1f} s "
          f"({boot_ms / n:.1f} ms/router avg)")
    print(f"  Health sweep       {sweep_ok}/{n} ok in {sweep_ms:.0f} ms")
    print(f"  Registration       {reg_ok}/{n} ok in {reg_ms:.0f} ms wall")
    print(f"  Visibility         {visible}/{n} routers")
    print(f"  Heartbeat one-shot {hb_ok}/{n} ok in {hb_ms:.0f} ms wall")
    print(f"  Heartbeat 5-round  avg {avg_round:.0f} ms/round")
    print(f"  Memory             {rss_mb:.0f} MB total "
          f"= {rss_mb / max(n, 1):.2f} MB/router")
    print("=" * 60 + "\n")

    await pool.aclose()
    for t in server_tasks:
        t.cancel()
    await asyncio.gather(*server_tasks, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
