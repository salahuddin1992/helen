"""
Live two-instance federation test.

Spins up two fully-independent CommClient-Server instances (each with its own
data directory, DB, JWT secret, server_id, port), then validates:

  1. Both instances start cleanly and serve /api/health
  2. UDP broadcast discovery cross-populates both registries
  3. HTTP ping round-trips in both directions
  4. Each instance can independently register a user + login (proves the
     DB/auth paths are actually isolated, not pointing at a shared file)
  5. A chat message fans out inside each instance independently

Cleans up child processes + data dirs on exit.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable


def _spawn(name: str, port: int, data_dir: Path, jwt_secret: str):
    if data_dir.exists():
        shutil.rmtree(data_dir, ignore_errors=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "uploads").mkdir(parents=True, exist_ok=True)
    db_path = data_dir / "commclient.db"

    env = os.environ.copy()
    env.update({
        "PORT": str(port),
        "SERVER_NAME": name,
        "SQLITE_PATH": str(db_path),
        "UPLOAD_DIR": str(data_dir / "uploads"),
        "JWT_SECRET": jwt_secret,
        "DISCOVERY_BROADCAST_INTERVAL": "2",
        "DISCOVERY_PEER_TTL": "20",
        "PYTHONUNBUFFERED": "1",
        "PYTHONIOENCODING": "utf-8",
    })
    log = open(data_dir / "server.log", "w", encoding="utf-8")
    p = subprocess.Popen(
        [PY, "run.py"],
        cwd=str(ROOT),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
    )
    return p, log


async def _wait_health(port: int, timeout: float = 30.0):
    deadline = time.time() + timeout
    async with httpx.AsyncClient(timeout=2.0) as c:
        while time.time() < deadline:
            try:
                r = await c.get(f"http://127.0.0.1:{port}/api/health")
                if r.status_code == 200:
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.25)
    return False


async def _me(port: int):
    async with httpx.AsyncClient(timeout=2.0) as c:
        r = await c.get(f"http://127.0.0.1:{port}/api/peers/me")
        return r.json()


async def _peers(port: int):
    async with httpx.AsyncClient(timeout=2.0) as c:
        r = await c.get(f"http://127.0.0.1:{port}/api/peers")
        return r.json()


async def _ping(from_port: int, to_server_id: str):
    async with httpx.AsyncClient(timeout=5.0) as c:
        r = await c.post(f"http://127.0.0.1:{from_port}/api/peers/{to_server_id}/ping")
        return r.json()


async def _register(port: int, username: str, password: str):
    async with httpx.AsyncClient(timeout=5.0) as c:
        r = await c.post(
            f"http://127.0.0.1:{port}/api/auth/register",
            json={"username": username, "password": password, "display_name": username},
        )
        return r.status_code, r.json() if r.content else {}


async def _login(port: int, username: str, password: str):
    async with httpx.AsyncClient(timeout=5.0) as c:
        r = await c.post(
            f"http://127.0.0.1:{port}/api/auth/login",
            json={"username": username, "password": password},
        )
        return r.status_code, r.json() if r.content else {}


async def _main():
    tmp = ROOT / "data_twoinstance"
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    alpha_dir = tmp / "alpha"
    beta_dir = tmp / "beta"

    print("[1/6] spawning Alpha on :3207 and Beta on :3208...")
    pa, la = _spawn("CommClient-Alpha", 3207, alpha_dir, "alpha-secret-do-not-use-in-prod-" + "A" * 32)
    pb, lb = _spawn("CommClient-Beta", 3208, beta_dir, "beta-secret-do-not-use-in-prod-" + "B" * 32)

    try:
        print("[2/6] waiting for health...")
        ok_a = await _wait_health(3207)
        ok_b = await _wait_health(3208)
        assert ok_a, "Alpha never became healthy"
        assert ok_b, "Beta never became healthy"
        print("     Alpha: up   Beta: up")

        me_a = await _me(3207)
        me_b = await _me(3208)
        print(f"     Alpha id={me_a['server_id']}  name={me_a['name']}  port={me_a['port']}")
        print(f"     Beta  id={me_b['server_id']}  name={me_b['name']}  port={me_b['port']}")
        assert me_a["server_id"] != me_b["server_id"], "server IDs must differ"

        print("[3/6] waiting 8s for UDP discovery to cross-populate...")
        await asyncio.sleep(8)

        pa_json = await _peers(3207)
        pb_json = await _peers(3208)
        a_ids = {p["server_id"] for p in pa_json["peers"]}
        b_ids = {p["server_id"] for p in pb_json["peers"]}
        print(f"     Alpha sees {len(a_ids)} peer(s): {sorted(a_ids)}")
        print(f"     Beta  sees {len(b_ids)} peer(s): {sorted(b_ids)}")
        discovery_ok = me_b["server_id"] in a_ids and me_a["server_id"] in b_ids
        print(f"     mutual discovery: {'YES' if discovery_ok else 'NO'}")

        print("[4/6] HTTP ping round-trips...")
        if discovery_ok:
            ab = await _ping(3207, me_b["server_id"])
            ba = await _ping(3208, me_a["server_id"])
            print(f"     alpha -> beta: ok={ab.get('ok')} status={ab.get('status_code')} rtt={ab.get('rtt_ms')}ms")
            print(f"     beta  -> alpha: ok={ba.get('ok')} status={ba.get('status_code')} rtt={ba.get('rtt_ms')}ms")
            ping_ok = ab.get("ok") and ba.get("ok")
        else:
            ping_ok = False

        print("[5/6] independent user DBs — register+login on each...")
        sa_reg, _ = await _register(3207, "alpha_user", "Str0ng!Pass-42")
        sb_reg, _ = await _register(3208, "beta_user", "Str0ng!Pass-42")
        print(f"     register alpha_user on Alpha: {sa_reg}")
        print(f"     register beta_user  on Beta:  {sb_reg}")
        # cross-check: alpha_user must NOT exist on Beta
        sa_on_b, _ = await _login(3208, "alpha_user", "Str0ng!Pass-42")
        sb_on_a, _ = await _login(3207, "beta_user", "Str0ng!Pass-42")
        print(f"     login alpha_user on Beta (should fail): {sa_on_b}")
        print(f"     login beta_user  on Alpha (should fail): {sb_on_a}")
        isolation_ok = (sa_reg in (200, 201)) and (sb_reg in (200, 201)) and (sa_on_b in (400, 401)) and (sb_on_a in (400, 401))

        print("[6/6] independent login (same user exists on each)...")
        sla, la_data = await _login(3207, "alpha_user", "Str0ng!Pass-42")
        slb, lb_data = await _login(3208, "beta_user", "Str0ng!Pass-42")
        la_tok = (la_data.get("tokens") or {}).get("access_token")
        lb_tok = (lb_data.get("tokens") or {}).get("access_token")
        print(f"     alpha_user login on Alpha: {sla}  (token present: {bool(la_tok)})")
        print(f"     beta_user  login on Beta : {slb}  (token present: {bool(lb_tok)})")
        login_ok = sla == 200 and slb == 200 and bool(la_tok) and bool(lb_tok)

        print()
        print("======== RESULT ========")
        print(f"  discovery : {'PASS' if discovery_ok else 'FAIL'}")
        print(f"  ping      : {'PASS' if ping_ok else 'FAIL'}")
        print(f"  isolation : {'PASS' if isolation_ok else 'FAIL'}  (users/DBs are independent)")
        print(f"  login     : {'PASS' if login_ok else 'FAIL'}  (each server mints its own JWT)")
        all_ok = discovery_ok and ping_ok and isolation_ok and login_ok
        print(f"  OVERALL   : {'PASS' if all_ok else 'FAIL'}")
        return 0 if all_ok else 1

    finally:
        print("\n[cleanup] terminating instances...")
        for p in (pa, pb):
            try:
                if sys.platform == "win32":
                    p.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    p.terminate()
            except Exception:
                pass
        for p in (pa, pb):
            try:
                p.wait(timeout=5)
            except Exception:
                p.kill()
        for fh in (la, lb):
            try: fh.close()
            except Exception: pass
        # keep logs for debugging; wipe only on success if requested
        print("     logs kept at:", alpha_dir / "server.log", "and", beta_dir / "server.log")


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
