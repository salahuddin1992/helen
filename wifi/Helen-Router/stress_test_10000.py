"""
Stress test: 10,000 diverse Helen-Router instances on a single host.

Diversity space:
  * 30 vendors (every major networking brand + custom + cloud)
  * 16 form factors (every router class from IoT to datacenter)
  * 8 sizes (Pico, Tiny, Small, Medium, Large, XL, Enterprise, HyperScale)
  * 5 styles (Wired, Wireless, Hybrid, Mesh, SDN)
  * 6 generations (Legacy, G3, G4, G5, G6, Quantum)

Total combination space = 30 × 16 × 8 × 5 × 6 = 115,200 unique profiles.

We deterministically pick 10,000 from this space (every router gets a
distinct profile shape).

Booting strategy
----------------
* Routers spawned in BATCHES of 500 to avoid simultaneous file-handle
  blow-up at startup.
* Per-batch warm-up wait: ~6 seconds for sockets to bind.
* Total boot time on a 16-core / 32 GB machine: ~6-10 minutes.
* RAM target: ~4-5 GB process RSS.

If your machine is smaller, lower N_ROUTERS or BATCH_SIZE.
"""

from __future__ import annotations

import asyncio
import secrets
import sys
import time
from collections import Counter

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route


# ── Tunables ────────────────────────────────────────────────────────

N_ROUTERS = 10_000
PORT_BASE = 30_000          # 30000..39999 — far from ephemeral range
BATCH_SIZE = 200            # smaller batches for better fd resilience
BATCH_PAUSE_SEC = 0.3
BOOT_TIMEOUT_SEC = 900.0    # 15 min ceiling for "all up"
SERVER_ID = "stress-server-" + secrets.token_hex(8)
SERVER_URL = "http://127.0.0.1:3000"


# ── Diversity catalog ────────────────────────────────────────────────

VENDORS = [
    "Cisco", "Juniper", "Mikrotik", "Ubiquiti", "TP-Link", "Huawei",
    "Aruba", "Fortinet", "OpenWrt", "pfSense", "Netgate", "MikroTik-CHR",
    "Arista", "Extreme", "Brocade", "Dell-Networking", "HPE",
    "Calix", "ZyXEL", "D-Link", "Linksys", "Asus", "Netgear",
    "Palo-Alto", "SonicWall", "Check-Point", "Sophos", "VyOS",
    "OPNsense", "Helen-Edge",
]
FORM_FACTORS = [
    "Edge", "Core", "Distribution", "Access", "IoT-Gateway",
    "Mesh-Node", "Branch", "Headend", "Border", "Aggregation",
    "Service-Provider", "Datacenter-Top-of-Rack", "Spine", "Leaf",
    "Provider-Edge", "Customer-Premises",
]
SIZES = ["Pico", "Tiny", "Small", "Medium", "Large", "XL",
         "Enterprise", "HyperScale"]
STYLES = ["Wired", "Wireless", "Hybrid", "Mesh", "SDN"]
GENERATIONS = ["Legacy", "G3", "G4", "G5", "G6", "Quantum"]


def assign_profile(idx: int) -> dict:
    return {
        "vendor": VENDORS[idx % len(VENDORS)],
        "form_factor": FORM_FACTORS[(idx // len(VENDORS))
                                    % len(FORM_FACTORS)],
        "size": SIZES[(idx // 19) % len(SIZES)],
        "style": STYLES[(idx // 31) % len(STYLES)],
        "generation": GENERATIONS[(idx // 53) % len(GENERATIONS)],
        "model": f"R-{idx:05d}",
        "serial": f"SN{idx:08d}",
    }


# ── Minimal router app (no FastAPI, just Starlette) ────────────────


def make_app(token: str, profile: dict) -> Starlette:
    upstreams: dict[str, dict] = {}

    def _check(req) -> bool:
        a = req.headers.get("authorization", "")
        return a.startswith("Bearer ") and secrets.compare_digest(
            a[7:], token
        )

    async def health(req):
        return JSONResponse({"status": "ok", "profile": profile,
                             "upstreams": len(upstreams)})

    async def list_up(req):
        now = time.time()
        return JSONResponse({"upstreams": [
            {**v, "stale_seconds": int(now - v["last_seen"])}
            for v in upstreams.values()
        ]})

    async def reg(req):
        if not _check(req):
            return JSONResponse({"error": "unauthorized"},
                                status_code=401)
        body = await req.json()
        sid, url = body.get("server_id"), body.get("url")
        if not sid or not url:
            return JSONResponse({"error": "missing"}, status_code=400)
        upstreams[sid] = {"id": sid, "url": url,
                          "capabilities": body.get("capabilities", []),
                          "last_seen": time.time()}
        return JSONResponse({"status": "registered"})

    async def hb(req):
        if not _check(req):
            return JSONResponse({"error": "unauthorized"},
                                status_code=401)
        sid = req.path_params["server_id"]
        if sid not in upstreams:
            return JSONResponse({"error": "unknown"}, status_code=404)
        upstreams[sid]["last_seen"] = time.time()
        return JSONResponse({"status": "ok"})

    return Starlette(routes=[
        Route("/router/health", health),
        Route("/router/upstreams", list_up),
        Route("/router/register", reg, methods=["POST"]),
        Route("/router/heartbeat/{server_id}", hb, methods=["POST"]),
    ])


async def run_one(port: int, token: str, profile: dict) -> None:
    cfg = uvicorn.Config(
        make_app(token, profile),
        host="127.0.0.1", port=port,
        log_level="critical", access_log=False, lifespan="off",
    )
    server = uvicorn.Server(cfg)
    # Patch uvicorn's startup so a single bind failure doesn't tear
    # down the whole asyncio loop with sys.exit(1). We just log + bail.
    orig_startup = server.startup
    async def _safe_startup(*a, **kw):
        try:
            await orig_startup(*a, **kw)
        except SystemExit:
            # uvicorn calls sys.exit(1) when create_server fails — we
            # convert that into a normal RuntimeError so this task
            # exits cleanly without killing the loop.
            raise RuntimeError(f"bind_failed_port_{port}")
    server.startup = _safe_startup
    try:
        await server.serve()
    except (RuntimeError, OSError):
        # Port collision / fd exhaustion / asyncio teardown — fine,
        # this individual router just doesn't come up. Probes will
        # detect it as dead and the script will report the count.
        return


async def wait_alive(port: int, deadline: float) -> bool:
    async with httpx.AsyncClient(timeout=0.4) as c:
        while time.perf_counter() < deadline:
            try:
                r = await c.get(f"http://127.0.0.1:{port}/router/health")
                if r.status_code == 200:
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.1)
    return False


def percentile(values, p):
    if not values:
        return 0.0
    s = sorted(values)
    return s[min(int(len(s) * p), len(s) - 1)]


# ── Main ────────────────────────────────────────────────────────────


async def main() -> None:
    print(f"[*] Stress test: {N_ROUTERS} routers on ports "
          f"{PORT_BASE}..{PORT_BASE + N_ROUTERS - 1}")
    print(f"    Batch size:  {BATCH_SIZE}")
    print(f"    Diversity:   {len(VENDORS)} vendors × "
          f"{len(FORM_FACTORS)} form factors × "
          f"{len(SIZES)} sizes × {len(STYLES)} styles × "
          f"{len(GENERATIONS)} generations\n")

    tokens = [secrets.token_hex(16) for _ in range(N_ROUTERS)]
    profiles = [assign_profile(i) for i in range(N_ROUTERS)]

    # Spawn in batches with brief pauses
    boot_t0 = time.perf_counter()
    server_tasks: list[asyncio.Task] = []
    n_batches = (N_ROUTERS + BATCH_SIZE - 1) // BATCH_SIZE
    for b in range(n_batches):
        start = b * BATCH_SIZE
        end = min(start + BATCH_SIZE, N_ROUTERS)
        for i in range(start, end):
            server_tasks.append(asyncio.create_task(
                run_one(PORT_BASE + i, tokens[i], profiles[i]),
                name=f"router-{i}",
            ))
        elapsed = time.perf_counter() - boot_t0
        print(f"    spawned batch {b + 1}/{n_batches} "
              f"({end} routers, {elapsed:.0f}s elapsed)", flush=True)
        await asyncio.sleep(BATCH_PAUSE_SEC)

    # Wait for all to be reachable
    print(f"\n[*] Probing health on all {N_ROUTERS} ports...", flush=True)
    deadline = time.perf_counter() + BOOT_TIMEOUT_SEC

    # Probe in chunks to keep memory + connections sane
    PROBE_CHUNK = 200
    healthy = 0
    chunk_t0 = time.perf_counter()
    for chunk_start in range(0, N_ROUTERS, PROBE_CHUNK):
        chunk_end = min(chunk_start + PROBE_CHUNK, N_ROUTERS)
        results = await asyncio.gather(*[
            wait_alive(PORT_BASE + i, deadline)
            for i in range(chunk_start, chunk_end)
        ], return_exceptions=True)
        chunk_ok = sum(1 for r in results if r is True)
        healthy += chunk_ok
        elapsed = time.perf_counter() - chunk_t0
        print(f"    chunk {chunk_start}..{chunk_end - 1}: "
              f"{chunk_ok}/{chunk_end - chunk_start} alive "
              f"(running total {healthy}/{N_ROUTERS}, {elapsed:.0f}s)",
              flush=True)

    boot_ms = (time.perf_counter() - boot_t0) * 1000
    print(f"\n[+] {healthy}/{N_ROUTERS} routers alive after "
          f"{boot_ms / 1000:.1f}s ({boot_ms / max(healthy, 1):.1f} ms avg)\n",
          flush=True)

    # Memory snapshot
    rss_mb = 0.0
    try:
        import psutil
        rss_mb = psutil.Process().memory_info().rss / 1024 / 1024
        print(f"[i] RSS after boot: {rss_mb:.0f} MB "
              f"({rss_mb / max(healthy, 1):.2f} MB/router)\n",
              flush=True)
    except Exception:
        pass

    if healthy < 100:
        print("[!] Too few routers up — aborting")
        return

    live = [i for i in range(N_ROUTERS)
            if i < N_ROUTERS]  # we'll filter dead ones via probes
    # We keep live = all indices; dead ones will just fail individual
    # requests below, but that's ok — we report success rate.

    pool = httpx.AsyncClient(
        timeout=httpx.Timeout(15.0, connect=5.0),
        limits=httpx.Limits(
            max_keepalive_connections=200,
            max_connections=400,
        ),
    )

    # ── Health-sweep on live indices ────────────────────────────
    print("[*] Parallel health sweep (chunked)...", flush=True)
    sweep_ok = 0
    vendor_count: Counter = Counter()
    size_count: Counter = Counter()
    gen_count: Counter = Counter()
    sweep_t0 = time.perf_counter()
    for cs in range(0, N_ROUTERS, PROBE_CHUNK):
        ce = min(cs + PROBE_CHUNK, N_ROUTERS)
        rs = await asyncio.gather(*[
            pool.get(f"http://127.0.0.1:{PORT_BASE + i}/router/health")
            for i in range(cs, ce)
        ], return_exceptions=True)
        for r in rs:
            if hasattr(r, "status_code") and r.status_code == 200:
                sweep_ok += 1
                try:
                    p = r.json().get("profile", {})
                    vendor_count[p.get("vendor")] += 1
                    size_count[p.get("size")] += 1
                    gen_count[p.get("generation")] += 1
                except Exception:
                    pass
    sweep_ms = (time.perf_counter() - sweep_t0) * 1000
    print(f"[+] Health sweep: {sweep_ok}/{N_ROUTERS} ok in "
          f"{sweep_ms / 1000:.1f}s\n", flush=True)
    print("    vendor distribution:", flush=True)
    for v, c in vendor_count.most_common():
        print(f"      {v:25s} {c}", flush=True)
    print("    size distribution:", flush=True)
    for s, c in size_count.most_common():
        print(f"      {s:25s} {c}", flush=True)
    print("    generation distribution:", flush=True)
    for g, c in gen_count.most_common():
        print(f"      {g:25s} {c}", flush=True)
    print()

    # ── Registration round ──────────────────────────────────────
    print(f"[*] Registering server at all {N_ROUTERS} routers...",
          flush=True)
    reg_ok, reg_lats = 0, []
    reg_t0 = time.perf_counter()

    async def reg_one(idx: int):
        t = time.perf_counter()
        try:
            r = await pool.post(
                f"http://127.0.0.1:{PORT_BASE + idx}/router/register",
                headers={"Authorization": f"Bearer {tokens[idx]}"},
                json={"server_id": SERVER_ID, "url": SERVER_URL,
                      "capabilities": ["rest", "socketio", "webrtc"]},
            )
            return r.status_code, (time.perf_counter() - t) * 1000
        except Exception:
            return 0, (time.perf_counter() - t) * 1000

    for cs in range(0, N_ROUTERS, PROBE_CHUNK):
        ce = min(cs + PROBE_CHUNK, N_ROUTERS)
        rs = await asyncio.gather(*[
            reg_one(i) for i in range(cs, ce)
        ], return_exceptions=True)
        for r in rs:
            if isinstance(r, tuple):
                if r[0] == 200:
                    reg_ok += 1
                reg_lats.append(r[1])
    reg_ms = (time.perf_counter() - reg_t0) * 1000
    print(f"[+] Registered {reg_ok}/{N_ROUTERS} in "
          f"{reg_ms / 1000:.1f}s wall  "
          f"(p50={percentile(reg_lats, 0.50):.0f} ms / "
          f"p95={percentile(reg_lats, 0.95):.0f} ms / "
          f"p99={percentile(reg_lats, 0.99):.0f} ms per req)\n",
          flush=True)

    # ── Heartbeat round ─────────────────────────────────────────
    print(f"[*] Heartbeat round to all {N_ROUTERS} routers...",
          flush=True)

    async def hb_one(idx: int):
        t = time.perf_counter()
        try:
            r = await pool.post(
                f"http://127.0.0.1:{PORT_BASE + idx}/router/heartbeat/{SERVER_ID}",
                headers={"Authorization": f"Bearer {tokens[idx]}"},
            )
            return r.status_code, (time.perf_counter() - t) * 1000
        except Exception:
            return 0, (time.perf_counter() - t) * 1000

    hb_t0 = time.perf_counter()
    hb_ok, hb_lats = 0, []
    for cs in range(0, N_ROUTERS, PROBE_CHUNK):
        ce = min(cs + PROBE_CHUNK, N_ROUTERS)
        rs = await asyncio.gather(*[
            hb_one(i) for i in range(cs, ce)
        ], return_exceptions=True)
        for r in rs:
            if isinstance(r, tuple):
                if r[0] == 200:
                    hb_ok += 1
                hb_lats.append(r[1])
    hb_ms = (time.perf_counter() - hb_t0) * 1000
    print(f"[+] Heartbeat: {hb_ok}/{N_ROUTERS} ok in "
          f"{hb_ms / 1000:.1f}s wall  "
          f"(p50={percentile(hb_lats, 0.50):.0f} ms / "
          f"p95={percentile(hb_lats, 0.95):.0f} ms / "
          f"p99={percentile(hb_lats, 0.99):.0f} ms per req)\n",
          flush=True)

    # 3 sustained heartbeat rounds
    print("[*] 3 sustained heartbeat rounds...", flush=True)
    rounds = []
    for i_round in range(3):
        rt0 = time.perf_counter()
        for cs in range(0, N_ROUTERS, PROBE_CHUNK):
            ce = min(cs + PROBE_CHUNK, N_ROUTERS)
            await asyncio.gather(*[hb_one(i) for i in range(cs, ce)],
                                 return_exceptions=True)
        rounds.append((time.perf_counter() - rt0) * 1000)
        print(f"    round {i_round + 1}: {rounds[-1] / 1000:.1f}s",
              flush=True)
    avg_round = sum(rounds) / len(rounds)
    print(f"[+] Sustained avg: {avg_round / 1000:.1f}s/round\n",
          flush=True)

    # Final memory
    try:
        import psutil
        rss_mb = psutil.Process().memory_info().rss / 1024 / 1024
        print(f"[i] Final RSS: {rss_mb:.0f} MB "
              f"({rss_mb / N_ROUTERS:.2f} MB/router)\n", flush=True)
    except Exception:
        pass

    print("=" * 65)
    print(f"  STRESS TEST SUMMARY ({N_ROUTERS} routers, "
          f"{len(VENDORS)} vendors)")
    print("=" * 65)
    print(f"  Boot time          {boot_ms / 1000:>7.1f} s "
          f"({boot_ms / N_ROUTERS:.1f} ms/router avg)")
    print(f"  Health sweep       {sweep_ok}/{N_ROUTERS} ok "
          f"({sweep_ms / 1000:.1f}s)")
    print(f"  Registration       {reg_ok}/{N_ROUTERS} ok "
          f"({reg_ms / 1000:.1f}s)")
    print(f"  Heartbeat one-shot {hb_ok}/{N_ROUTERS} ok "
          f"({hb_ms / 1000:.1f}s)")
    print(f"  Heartbeat 3-round  avg {avg_round / 1000:.1f}s/round")
    print(f"  Memory             {rss_mb:.0f} MB total "
          f"= {rss_mb / N_ROUTERS:.2f} MB/router")
    print("=" * 65 + "\n")

    await pool.aclose()
    for t in server_tasks:
        t.cancel()
    await asyncio.gather(*server_tasks, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
