"""
Stress test: spawn 100 minimal Helen-Router instances in a single
asyncio process, then have a simulated server register against all 100
in parallel. Measures boot time, registration latency, visibility,
heartbeat, and per-router memory.

The minimal router uses Starlette directly (no FastAPI/Pydantic
overhead) — the 100-instance amplification would otherwise dominate
RAM. The wire protocol is identical to the production router so a
real Helen-Server can register against these stubs unmodified.
"""

from __future__ import annotations

import asyncio
import secrets
import time

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route


N_ROUTERS = 100
PORT_BASE = 8100


def make_router_app(token: str) -> Starlette:
    """Produce a fresh router app with its own state."""
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
        sid = body.get("server_id")
        url = body.get("url")
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

    async def unregister(req):
        if not _check(req):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        upstreams.pop(req.path_params["server_id"], None)
        return JSONResponse({"status": "unregistered"})

    return Starlette(routes=[
        Route("/router/health", health),
        Route("/router/upstreams", list_upstreams),
        Route("/router/register", register, methods=["POST"]),
        Route("/router/heartbeat/{server_id}", heartbeat,
              methods=["POST"]),
        Route("/router/register/{server_id}", unregister,
              methods=["DELETE"]),
    ])


async def run_one_router(port: int, token: str) -> None:
    config = uvicorn.Config(
        make_router_app(token),
        host="127.0.0.1", port=port,
        log_level="error", access_log=False,
        lifespan="off",
    )
    server = uvicorn.Server(config)
    await server.serve()


async def wait_for_health(port: int, timeout_sec: float = 30.0) -> bool:
    deadline = time.perf_counter() + timeout_sec
    async with httpx.AsyncClient(timeout=0.5) as c:
        while time.perf_counter() < deadline:
            try:
                r = await c.get(
                    f"http://127.0.0.1:{port}/router/health"
                )
                if r.status_code == 200:
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.05)
    return False


async def main() -> None:
    print(f"[*] Spawning {N_ROUTERS} routers on ports "
          f"{PORT_BASE}..{PORT_BASE + N_ROUTERS - 1}...")

    tokens = [secrets.token_hex(16) for _ in range(N_ROUTERS)]

    t0 = time.perf_counter()
    server_tasks = [
        asyncio.create_task(
            run_one_router(PORT_BASE + i, tokens[i]),
            name=f"router-{i}",
        )
        for i in range(N_ROUTERS)
    ]

    # Wait for all to be accepting
    print("[*] Waiting for routers to come up...")
    health_results = await asyncio.gather(
        *[wait_for_health(PORT_BASE + i) for i in range(N_ROUTERS)],
        return_exceptions=True,
    )
    boot_ms = (time.perf_counter() - t0) * 1000
    healthy = sum(1 for r in health_results if r is True)
    print(f"[+] {healthy}/{N_ROUTERS} routers up in {boot_ms:.0f} ms "
          f"({boot_ms / max(healthy, 1):.1f} ms avg)")

    if healthy < N_ROUTERS:
        print(f"[!] {N_ROUTERS - healthy} routers failed to start — aborting")
        return

    # Health-check sweep
    t0 = time.perf_counter()
    async with httpx.AsyncClient(timeout=2.0) as c:
        sweep = await asyncio.gather(*[
            c.get(f"http://127.0.0.1:{PORT_BASE + i}/router/health")
            for i in range(N_ROUTERS)
        ], return_exceptions=True)
    sweep_ok = sum(
        1 for r in sweep
        if hasattr(r, "status_code") and r.status_code == 200
    )
    sweep_ms = (time.perf_counter() - t0) * 1000
    print(f"[+] Parallel health sweep: {sweep_ok}/{N_ROUTERS} ok in "
          f"{sweep_ms:.0f} ms")

    # ── Server registers against all 100 ────────────────────────
    print(f"\n[*] Server registering at all {N_ROUTERS} routers in parallel...")
    server_id = "stress-server-" + secrets.token_hex(8)
    self_url = "http://127.0.0.1:3000"

    # Single shared httpx client — connection pool is reused across
    # all 100 requests instead of spinning up a fresh TCP socket for
    # each one (which dominated the previous 16s wall clock).
    pool = httpx.AsyncClient(
        timeout=httpx.Timeout(2.0, connect=1.0),
        limits=httpx.Limits(
            max_keepalive_connections=N_ROUTERS,
            max_connections=N_ROUTERS * 2,
        ),
    )

    async def register_at(idx: int) -> tuple[int, int, float]:
        t = time.perf_counter()
        r = await pool.post(
            f"http://127.0.0.1:{PORT_BASE + idx}/router/register",
            headers={"Authorization": f"Bearer {tokens[idx]}"},
            json={"server_id": server_id, "url": self_url,
                  "capabilities": ["rest", "socketio"]},
        )
        return idx, r.status_code, (time.perf_counter() - t) * 1000

    t0 = time.perf_counter()
    reg_results = await asyncio.gather(*[
        register_at(i) for i in range(N_ROUTERS)
    ], return_exceptions=True)
    reg_ms = (time.perf_counter() - t0) * 1000

    reg_ok = sum(
        1 for r in reg_results
        if isinstance(r, tuple) and r[1] == 200
    )
    per_req = sorted([r[2] for r in reg_results if isinstance(r, tuple)])
    p50 = per_req[len(per_req) // 2] if per_req else 0
    p95 = per_req[int(len(per_req) * 0.95)] if per_req else 0
    p99 = per_req[int(len(per_req) * 0.99)] if per_req else 0
    print(f"[+] Registered {reg_ok}/{N_ROUTERS} in {reg_ms:.0f} ms wall "
          f"(p50={p50:.1f} p95={p95:.1f} p99={p99:.1f} per req)")

    # Visibility — every router should now know about us
    print(f"\n[*] Verifying visibility at every router...")
    t0 = time.perf_counter()
    async with httpx.AsyncClient(timeout=2.0) as c:
        seen = await asyncio.gather(*[
            c.get(f"http://127.0.0.1:{PORT_BASE + i}/router/upstreams")
            for i in range(N_ROUTERS)
        ], return_exceptions=True)
    visible = sum(
        1 for r in seen
        if hasattr(r, "status_code")
        and r.status_code == 200
        and server_id in r.text
    )
    verify_ms = (time.perf_counter() - t0) * 1000
    print(f"[+] Visible at {visible}/{N_ROUTERS} in {verify_ms:.0f} ms")

    # Heartbeat round (shared pool too)
    print(f"\n[*] One heartbeat round to all {N_ROUTERS} routers...")

    async def hb(idx: int) -> tuple[int, float]:
        t = time.perf_counter()
        r = await pool.post(
            f"http://127.0.0.1:{PORT_BASE + idx}/router/heartbeat/{server_id}",
            headers={"Authorization": f"Bearer {tokens[idx]}"},
        )
        return r.status_code, (time.perf_counter() - t) * 1000

    t0 = time.perf_counter()
    hb_results = await asyncio.gather(
        *[hb(i) for i in range(N_ROUTERS)], return_exceptions=True
    )
    hb_ms = (time.perf_counter() - t0) * 1000
    hb_ok = sum(
        1 for r in hb_results
        if isinstance(r, tuple) and r[0] == 200
    )
    hb_lat = sorted([r[1] for r in hb_results if isinstance(r, tuple)])
    hb_p50 = hb_lat[len(hb_lat) // 2] if hb_lat else 0
    hb_p95 = hb_lat[int(len(hb_lat) * 0.95)] if hb_lat else 0
    print(f"[+] Heartbeat: {hb_ok}/{N_ROUTERS} ok in {hb_ms:.0f} ms wall "
          f"(p50={hb_p50:.1f} p95={hb_p95:.1f} per req)")

    # 10 sustained heartbeat rounds
    print(f"\n[*] 10 sustained heartbeat rounds...")
    rounds = []
    for r_idx in range(10):
        t0 = time.perf_counter()
        await asyncio.gather(*[hb(i) for i in range(N_ROUTERS)])
        rounds.append((time.perf_counter() - t0) * 1000)
    avg_round = sum(rounds) / len(rounds)
    print(f"[+] 10 rounds avg = {avg_round:.0f} ms "
          f"(min={min(rounds):.0f} max={max(rounds):.0f})")

    # Memory snapshot
    try:
        import psutil
        proc = psutil.Process()
        rss_mb = proc.memory_info().rss / 1024 / 1024
        print(f"\n[i] Process RSS: {rss_mb:.1f} MB "
              f"({rss_mb / N_ROUTERS:.2f} MB per router)")
    except Exception:
        pass

    print("\n" + "=" * 50)
    print(f"  STRESS TEST SUMMARY ({N_ROUTERS} routers)")
    print("=" * 50)
    print(f"  Boot all routers:    {boot_ms:.0f} ms "
          f"({boot_ms / N_ROUTERS:.1f} ms avg)")
    print(f"  Health sweep:        {sweep_ok}/{N_ROUTERS} in {sweep_ms:.0f} ms")
    print(f"  Server register:     {reg_ok}/{N_ROUTERS} in {reg_ms:.0f} ms")
    print(f"  Server visible at:   {visible}/{N_ROUTERS} routers")
    print(f"  Heartbeat one-shot:  {hb_ok}/{N_ROUTERS} in {hb_ms:.0f} ms")
    print(f"  Heartbeat sustained: {avg_round:.0f} ms/round avg")
    print("=" * 50 + "\n")

    # Tear down
    await pool.aclose()
    for t in server_tasks:
        t.cancel()
    await asyncio.gather(*server_tasks, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
