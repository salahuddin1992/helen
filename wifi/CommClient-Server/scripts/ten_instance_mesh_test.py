"""
10-instance LAN federation mesh test.

Spins up 10 fully-isolated CommClient-Server instances (own port, own DB,
own data dir, own JWT secret, own server_id), then verifies:

  1. All 10 reach /api/health
  2. Each instance sees every OTHER instance via UDP broadcast
     (full N*(N-1) mesh)
  3. A sample of cross-pings (each server pings one random peer) all succeed
     with reasonable RTT

Prints a discovery matrix so failures are visible at a glance.
"""

from __future__ import annotations

import asyncio
import os
import random
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
N = 10
BASE_PORT = 3301


def _spawn(idx: int, data_dir: Path):
    port = BASE_PORT + idx
    name = f"CommClient-N{idx:02d}"
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
        "JWT_SECRET": f"instance-{idx}-secret-" + "x" * 40,
        "DISCOVERY_BROADCAST_INTERVAL": "2",
        "DISCOVERY_PEER_TTL": "30",
        "PYTHONUNBUFFERED": "1",
        "PYTHONIOENCODING": "utf-8",
    })
    log = open(data_dir / "server.log", "w", encoding="utf-8")
    kwargs: dict = dict(
        cwd=str(ROOT), env=env,
        stdout=log, stderr=subprocess.STDOUT,
    )
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    p = subprocess.Popen([PY, "run.py"], **kwargs)
    return port, name, p, log


async def _wait_health(port: int, timeout: float = 45.0):
    deadline = time.time() + timeout
    async with httpx.AsyncClient(timeout=2.0) as c:
        while time.time() < deadline:
            try:
                r = await c.get(f"http://127.0.0.1:{port}/api/health")
                if r.status_code == 200:
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.5)
    return False


async def _me(port: int):
    async with httpx.AsyncClient(timeout=3.0) as c:
        r = await c.get(f"http://127.0.0.1:{port}/api/peers/me")
        return r.json()


async def _peers(port: int):
    async with httpx.AsyncClient(timeout=3.0) as c:
        r = await c.get(f"http://127.0.0.1:{port}/api/peers")
        return r.json()


async def _ping(from_port: int, to_server_id: str):
    async with httpx.AsyncClient(timeout=5.0) as c:
        r = await c.post(f"http://127.0.0.1:{from_port}/api/peers/{to_server_id}/ping")
        return r.json()


async def _main():
    tmp = ROOT / "data_mesh"
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"[1/5] spawning {N} instances on ports {BASE_PORT}..{BASE_PORT + N - 1}")
    instances = []
    for i in range(N):
        port, name, p, log = _spawn(i, tmp / f"node{i:02d}")
        instances.append({"idx": i, "port": port, "name": name, "proc": p, "log": log})
        print(f"     spawn {name} :{port}")

    try:
        print(f"[2/5] waiting for all {N} to reach /api/health...")
        t0 = time.time()
        ok = await asyncio.gather(*[_wait_health(inst["port"]) for inst in instances])
        healthy = sum(ok)
        print(f"     {healthy}/{N} healthy in {time.time() - t0:.1f}s")
        if healthy < N:
            for inst, up in zip(instances, ok):
                if not up:
                    print(f"     FAIL: {inst['name']} :{inst['port']}")
            return 1

        mes = await asyncio.gather(*[_me(inst["port"]) for inst in instances])
        for inst, m in zip(instances, mes):
            inst["server_id"] = m["server_id"]
            print(f"     {inst['name']}: id={inst['server_id']}")
        ids = {inst["server_id"] for inst in instances}
        assert len(ids) == N, f"server_id collision! got {len(ids)} unique IDs for {N} instances"

        print(f"[3/5] waiting 15s for UDP discovery to converge across {N} nodes...")
        await asyncio.sleep(15)

        print(f"[4/5] discovery matrix — each row is what node X sees:")
        peer_lists = await asyncio.gather(*[_peers(inst["port"]) for inst in instances])
        id_to_inst = {inst["server_id"]: inst for inst in instances}

        hdr = "     " + "".join(f" N{inst['idx']:02d}" for inst in instances)
        print(hdr)
        total_expected = N * (N - 1)
        total_seen = 0
        for src, peers_json in zip(instances, peer_lists):
            seen_ids = {p["server_id"] for p in peers_json["peers"]}
            row = f"  N{src['idx']:02d}"
            for dst in instances:
                if dst["idx"] == src["idx"]:
                    cell = "  . "  # self
                elif dst["server_id"] in seen_ids:
                    cell = "  + "
                    total_seen += 1
                else:
                    cell = "  - "
                row += cell
            print(row)
        print(f"     total cross-pairs: {total_seen}/{total_expected}")
        discovery_full = (total_seen == total_expected)

        print(f"[5/5] random cross-pings (each node pings one other)...")
        ping_results = []
        random.seed(0)
        for src in instances:
            others = [i for i in instances if i["idx"] != src["idx"]]
            dst = random.choice(others)
            r = await _ping(src["port"], dst["server_id"])
            ping_results.append((src, dst, r))
            ok_s = r.get("ok")
            rtt = r.get("rtt_ms")
            tag = "PASS" if ok_s else "FAIL"
            print(f"     {src['name']} -> {dst['name']}: {tag}  rtt={rtt}ms  status={r.get('status_code')}")
        ping_ok = all(r.get("ok") for _, _, r in ping_results)

        print()
        print("========= RESULT =========")
        print(f"  instances healthy   : {healthy}/{N}")
        print(f"  unique server_ids   : {len(ids)}/{N}")
        print(f"  discovery cross-pairs: {total_seen}/{total_expected}  {'PASS' if discovery_full else 'FAIL'}")
        print(f"  random ping mesh     : {sum(1 for _,_,r in ping_results if r.get('ok'))}/{N}  {'PASS' if ping_ok else 'FAIL'}")
        print(f"  OVERALL              : {'PASS' if (discovery_full and ping_ok) else 'FAIL'}")
        return 0 if (discovery_full and ping_ok) else 1

    finally:
        print("\n[cleanup] terminating instances...")
        for inst in instances:
            try:
                if sys.platform == "win32":
                    inst["proc"].send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    inst["proc"].terminate()
            except Exception:
                pass
        for inst in instances:
            try:
                inst["proc"].wait(timeout=4)
            except Exception:
                try: inst["proc"].kill()
                except Exception: pass
        for inst in instances:
            try: inst["log"].close()
            except Exception: pass


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
