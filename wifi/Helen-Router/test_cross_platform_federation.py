"""
Cross-platform server federation test.

Verifies that two Helen-Server instances can talk to each other
through Helen-Router regardless of host OS. We can't actually boot
a Linux ELF on a Windows host, so we simulate the cross-platform
case by:

  * Server A: the production Windows .exe (real binary).
  * Server B: the same Python source code run via the Windows venv,
    with a different env (PORT + DATA_DIR) — semantically what a
    Linux Helen-Server would look like, since both flavours are
    built from the same Python source.

Then we drive a router in front of both and verify:

  1. Both register at the router successfully.
  2. The router round-robins / closest-first picks between them.
  3. A request through the router reaches whichever upstream the
     broker chose.
  4. Killing one upstream → the other still serves.
  5. Both upstreams report identical /api/health JSON shape.

The wire-protocol bytes are independent of OS — what we're really
testing is "does the same code path work when one side is on a
different host kind". If yes, Linux ↔ Windows federation works.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

import httpx


SERVER_EXE = (
    Path(__file__).parent.parent
    / "CommClient-Server" / "dist" / "Helen-Server" / "Helen-Server.exe"
)
SERVER_SOURCE = Path(__file__).parent.parent / "CommClient-Server"
VENV_PYTHON = (
    Path(__file__).parent.parent / "CommClient-Server" / "venv"
    / "Scripts" / "python.exe"
)


def banner(s: str) -> None:
    print("\n" + "═" * 64)
    print(f"  {s}")
    print("═" * 64)


async def wait_health(url: str, timeout_sec: float = 30.0) -> bool:
    deadline = time.perf_counter() + timeout_sec
    async with httpx.AsyncClient(timeout=2.0) as c:
        while time.perf_counter() < deadline:
            try:
                r = await c.get(f"{url}/api/health")
                if r.status_code == 200:
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.5)
    return False


async def main() -> None:
    if not SERVER_EXE.exists():
        print(f"[!] {SERVER_EXE} not found")
        sys.exit(1)

    banner("STAGE 1 — start two Helen-Server instances")
    win_dir = SERVER_EXE.parent
    jwt_a = secrets.token_hex(32)
    jwt_b = secrets.token_hex(32)
    router_token = secrets.token_hex(32)

    # Server A — real Windows .exe
    print("[*] Starting server-A (Windows .exe) on port 3010...")
    env_a = {
        **os.environ,
        "JWT_SECRET": jwt_a,
        "PORT": "3010",
        "DEBUG": "0",
    }
    proc_a = subprocess.Popen(
        [str(SERVER_EXE)],
        cwd=str(win_dir),
        env=env_a,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Server B — Python source ("Linux-style") on port 3011
    print("[*] Starting server-B (Python source / Linux-style) on port 3011...")
    env_b = {
        **os.environ,
        "JWT_SECRET": jwt_b,
        "PORT": "3011",
        "DEBUG": "0",
        # Force a separate data dir so the two servers don't fight over SQLite
        "SQLITE_PATH": str(Path(os.environ.get("TEMP", "/tmp"))
                            / f"helen-b-{secrets.token_hex(4)}.db"),
    }
    proc_b = subprocess.Popen(
        [str(VENV_PYTHON), "run.py"],
        cwd=str(SERVER_SOURCE),
        env=env_b,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    print("[*] Waiting for both health endpoints...")
    a_ok = await wait_health("http://127.0.0.1:3010", timeout_sec=40)
    b_ok = await wait_health("http://127.0.0.1:3011", timeout_sec=40)
    print(f"  server-A health: {'OK' if a_ok else 'TIMEOUT'}")
    print(f"  server-B health: {'OK' if b_ok else 'TIMEOUT'}")
    if not (a_ok and b_ok):
        for p in (proc_a, proc_b):
            p.terminate()
        sys.exit(1)

    banner("STAGE 2 — verify wire-protocol parity")
    async with httpx.AsyncClient(timeout=3.0) as c:
        ra = await c.get("http://127.0.0.1:3010/api/health")
        rb = await c.get("http://127.0.0.1:3011/api/health")
    print(f"  server-A /api/health body: {ra.text[:120]}")
    print(f"  server-B /api/health body: {rb.text[:120]}")
    body_a = ra.json()
    body_b = rb.json()
    same_shape = set(body_a.keys()) == set(body_b.keys())
    print(f"  identical JSON shape: {same_shape}")
    print(f"  same service name:    {body_a['service'] == body_b['service']}")
    print(f"  same version:         {body_a['version'] == body_b['version']}")

    banner("STAGE 3 — start router and register both servers")
    router_proc = subprocess.Popen(
        [str(VENV_PYTHON), "run.py"],
        cwd=str(Path(__file__).parent),
        env={
            **os.environ,
            "HELEN_ROUTER_TOKEN": router_token,
            "HELEN_ROUTER_PORT": "8200",
            "HELEN_ROUTER_DISABLE_MDNS": "1",
        },
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print("[*] Waiting for router...")
    await asyncio.sleep(4.0)
    async with httpx.AsyncClient(timeout=2.0) as c:
        try:
            rh = await c.get("http://127.0.0.1:8200/router/health")
            print(f"  router health: {rh.json()}")
        except Exception as e:
            print(f"  router health: FAIL ({e})")
            for p in (proc_a, proc_b, router_proc):
                p.terminate()
            sys.exit(1)

        # Register both servers
        for sid, port in [("server-A", 3010), ("server-B", 3011)]:
            r = await c.post(
                "http://127.0.0.1:8200/router/register",
                headers={"Authorization": f"Bearer {router_token}"},
                json={
                    "server_id": sid,
                    "url": f"http://127.0.0.1:{port}",
                    "capabilities": ["rest", "socketio"],
                },
            )
            print(f"  register {sid}: HTTP {r.status_code}")

        # List upstreams
        ups = await c.get("http://127.0.0.1:8200/router/upstreams")
        print(f"\n  /router/upstreams: {len(ups.json()['upstreams'])} entries")
        for u in ups.json()["upstreams"]:
            print(f"    • {u['id']}  {u['url']}")

    banner("STAGE 4 — request through the router (failover chain)")
    async with httpx.AsyncClient(timeout=3.0) as c:
        for i in range(3):
            r = await c.get("http://127.0.0.1:8200/api/health")
            served = r.headers.get("X-Helen-Upstream", "?")
            print(f"  call {i+1}: HTTP {r.status_code}  served by {served}")

    banner("STAGE 5 — kill server-A; router must failover to server-B")
    print("[*] Killing server-A...")
    proc_a.terminate()
    proc_a.wait(timeout=5)
    await asyncio.sleep(11.0)  # let RTT prober mark A unreachable

    async with httpx.AsyncClient(timeout=5.0) as c:
        for i in range(3):
            r = await c.get("http://127.0.0.1:8200/api/health")
            served = r.headers.get("X-Helen-Upstream", "?")
            print(f"  post-kill call {i+1}: HTTP {r.status_code}  served by {served}")

    banner("STAGE 6 — kill server-B; router must return all_upstreams_unreachable")
    print("[*] Killing server-B...")
    proc_b.terminate()
    proc_b.wait(timeout=5)
    await asyncio.sleep(11.0)

    async with httpx.AsyncClient(timeout=5.0) as c:
        r = await c.get("http://127.0.0.1:8200/api/health")
        # NOTE: /api/health is in the bypass list of RouterRequiredMiddleware,
        # but the router itself proxies it — so we get whatever the upstream
        # said. With both upstreams gone we expect 503 / all_upstreams_unreachable.
        print(f"  router with no upstreams: HTTP {r.status_code} {r.text[:100]}")

    banner("CLEANUP")
    router_proc.terminate()
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
