"""
1000-instance stress test — pushes the machine's limits.

Strategy:
  1. Spawn servers in batches of 50 with staggered delay to let the OS breathe
  2. After each batch, check free RAM; abort spawning if below 4 GB
  3. Track how many reached healthy status and how many the mesh discovered
  4. On a 50-instance sample, run the full within-server workflow
     (registration + chat + call + file upload)
  5. Report what the machine actually could sustain

This deliberately does NOT attempt the full 1000-instance workflow loop —
1000 sequential HTTP+WebSocket roundtrips from one python process would
itself become the bottleneck. We focus on:
  - max concurrent instances that reach healthy
  - mesh discovery convergence at that density
  - functional correctness on a representative sample
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
import psutil  # type: ignore  # may not be installed — handled below

try:
    import socketio  # type: ignore
except ImportError:
    print("FATAL: python-socketio missing", file=sys.stderr); sys.exit(1)


ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
TARGET_N = int(os.environ.get("N", "1000"))
BASE_PORT = int(os.environ.get("BASE_PORT", "4000"))
BATCH_SPAWN = int(os.environ.get("BATCH_SPAWN", "50"))
SPAWN_BATCH_PAUSE = float(os.environ.get("SPAWN_BATCH_PAUSE", "2.5"))
MIN_FREE_MB = int(os.environ.get("MIN_FREE_MB", "4096"))  # abort spawning below this
SAMPLE_WORKFLOW = int(os.environ.get("SAMPLE_WORKFLOW", "50"))
DISCOVERY_WAIT = int(os.environ.get("DISCOVERY_WAIT", "45"))


def _free_mb() -> int:
    return psutil.virtual_memory().available // (1024 * 1024)


def _spawn(idx: int, data_dir: Path):
    port = BASE_PORT + idx
    name = f"K{idx:04d}"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "uploads").mkdir(parents=True, exist_ok=True)
    db_path = data_dir / "commclient.db"

    env = os.environ.copy()
    env.update({
        "PORT": str(port),
        "SERVER_NAME": name,
        "SQLITE_PATH": str(db_path),
        "UPLOAD_DIR": str(data_dir / "uploads"),
        "JWT_SECRET": f"inst-{idx}-secret-" + "x" * 40,
        "DISCOVERY_BROADCAST_INTERVAL": "3",
        "DISCOVERY_PEER_TTL": "60",
        "PYTHONUNBUFFERED": "1",
        "PYTHONIOENCODING": "utf-8",
    })
    log = open(data_dir / "server.log", "w", encoding="utf-8")
    kwargs: dict = dict(cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT)
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    p = subprocess.Popen([PY, "run.py"], **kwargs)
    return port, name, p, log


async def _wait_health(port: int, timeout: float = 180.0) -> bool:
    deadline = time.time() + timeout
    async with httpx.AsyncClient(timeout=2.5) as c:
        while time.time() < deadline:
            try:
                r = await c.get(f"http://127.0.0.1:{port}/api/health")
                if r.status_code == 200:
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.6)
    return False


async def _peers_count(port: int) -> int:
    try:
        async with httpx.AsyncClient(timeout=4.0) as c:
            r = await c.get(f"http://127.0.0.1:{port}/api/peers")
            if r.status_code == 200:
                return len(r.json().get("peers", []))
    except Exception:
        pass
    return -1


async def _register(port: int, username: str):
    try:
        async with httpx.AsyncClient(timeout=8.0) as c:
            r = await c.post(
                f"http://127.0.0.1:{port}/api/auth/register",
                json={"username": username, "password": "Str0ng!Pass-42", "display_name": username},
            )
            if r.status_code in (200, 201):
                j = r.json()
                tok = (j.get("tokens") or {}).get("access_token") or j.get("access_token")
                uid = (j.get("user") or {}).get("id") or j.get("user_id")
                return tok, uid
    except Exception:
        pass
    return None, None


async def _create_channel(port: int, token: str, members: list[str]):
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.post(
                f"http://127.0.0.1:{port}/api/channels",
                headers={"Authorization": f"Bearer {token}"},
                json={"type": "group", "name": f"g-{random.randint(0, 1 << 24):x}", "member_ids": members},
            )
            if r.status_code in (200, 201):
                return r.json().get("id")
    except Exception:
        pass
    return None


async def _upload_file(port: int, token: str, channel_id: str) -> bool:
    try:
        body = ("1k-mesh-" + "x" * 256).encode()
        async with httpx.AsyncClient(timeout=12.0) as c:
            r = await c.post(
                f"http://127.0.0.1:{port}/api/files/upload",
                headers={"Authorization": f"Bearer {token}"},
                files={"file": ("probe.txt", body, "text/plain")},
                data={"channel_id": channel_id},
            )
            return r.status_code in (200, 201)
    except Exception:
        pass
    return False


async def _socket_flow(port: int, a_tok: str, b_tok: str, channel_id: str) -> dict:
    import socketio as sio_mod
    r = {k: False for k in ["sock_a", "sock_b", "join_a", "join_b", "chat_sent",
                            "chat_received", "call_init", "call_hang"]}
    sa = sio_mod.AsyncClient(reconnection=False)
    sb = sio_mod.AsyncClient(reconnection=False)
    url = f"http://127.0.0.1:{port}"
    got_chat = asyncio.Event()

    @sb.on("v2_chat:new_message")
    async def _on_chat(p):
        if p and p.get("channel_id") == channel_id:
            got_chat.set()

    try:
        await asyncio.wait_for(sa.connect(url, auth={"token": a_tok}, transports=["websocket"]), timeout=10)
        r["sock_a"] = True
        await asyncio.wait_for(sb.connect(url, auth={"token": b_tok}, transports=["websocket"]), timeout=10)
        r["sock_b"] = True

        ja = await asyncio.wait_for(sa.call("v2_call_join_group", {"channel_id": channel_id, "media_type": "audio"}), timeout=12)
        r["join_a"] = isinstance(ja, dict) and not ja.get("error")
        cid = (ja or {}).get("call_id")

        jb = await asyncio.wait_for(sb.call("v2_call_join_group", {"channel_id": channel_id, "media_type": "audio"}), timeout=12)
        r["join_b"] = isinstance(jb, dict) and not jb.get("error")

        chat = await asyncio.wait_for(sa.call("v2_chat_send_message", {
            "channel_id": channel_id, "content": "1k-test", "type": "text",
            "client_id": f"k-{random.randint(0, 1 << 24):x}",
        }), timeout=12)
        r["chat_sent"] = isinstance(chat, dict) and chat.get("message_id")

        try:
            await asyncio.wait_for(got_chat.wait(), timeout=8)
            r["chat_received"] = True
        except asyncio.TimeoutError:
            r["chat_received"] = False

        if cid:
            r["call_init"] = True
            hang = await asyncio.wait_for(sa.call("v2_call_hangup", {"call_id": cid}), timeout=10)
            r["call_hang"] = not (isinstance(hang, dict) and hang.get("error"))
    except Exception:
        pass
    finally:
        try: await sa.disconnect()
        except Exception: pass
        try: await sb.disconnect()
        except Exception: pass
    return r


async def _workflow(port: int, idx: int) -> dict:
    steps: dict[str, bool] = {}
    a_tok, _ = await _register(port, f"a_{idx}")
    b_tok, b_uid = await _register(port, f"b_{idx}")
    steps["reg_a"] = bool(a_tok)
    steps["reg_b"] = bool(b_tok and b_uid)
    if not (a_tok and b_tok and b_uid):
        return steps
    ch_id = await _create_channel(port, a_tok, [b_uid])
    steps["channel"] = bool(ch_id)
    if not ch_id:
        return steps
    steps.update(await _socket_flow(port, a_tok, b_tok, ch_id))
    steps["file_upload"] = await _upload_file(port, a_tok, ch_id)
    return steps


async def _cleanup(instances):
    print(f"\n[cleanup] terminating {len(instances)} instances...")
    t0 = time.time()
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
            inst["proc"].wait(timeout=5)
        except Exception:
            try: inst["proc"].kill()
            except Exception: pass
    for inst in instances:
        try: inst["log"].close()
        except Exception: pass
    print(f"     cleanup complete in {time.time() - t0:.1f}s")


async def _main() -> int:
    tmp = ROOT / "data_mesh1k"
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"[1/5] spawning up to {TARGET_N} instances on ports {BASE_PORT}..{BASE_PORT + TARGET_N - 1}")
    print(f"      safety: abort when free RAM < {MIN_FREE_MB} MB")
    print(f"      starting free RAM: {_free_mb()} MB")
    instances = []
    spawned = 0
    aborted_at = None
    try:
        for batch_start in range(0, TARGET_N, BATCH_SPAWN):
            free = _free_mb()
            if free < MIN_FREE_MB:
                aborted_at = spawned
                print(f"     ABORT: free RAM {free} MB < {MIN_FREE_MB} MB at {spawned} instances")
                break
            batch_end = min(batch_start + BATCH_SPAWN, TARGET_N)
            for i in range(batch_start, batch_end):
                port, name, p, log = _spawn(i, tmp / f"k{i:04d}")
                instances.append({"idx": i, "port": port, "name": name, "proc": p, "log": log})
            spawned = len(instances)
            print(f"     spawned {spawned}/{TARGET_N}   free_ram={_free_mb()} MB   elapsed_batch")
            await asyncio.sleep(SPAWN_BATCH_PAUSE)
        print(f"     spawn done: {spawned} instances, free_ram={_free_mb()} MB")

        print(f"[2/5] waiting for all {spawned} to become healthy (giving up on stragglers after 180s)...")
        t0 = time.time()
        results = await asyncio.gather(*[_wait_health(inst["port"]) for inst in instances])
        healthy = sum(results)
        print(f"     {healthy}/{spawned} healthy in {time.time() - t0:.1f}s")

        if healthy == 0:
            print("     FATAL: no instances healthy; aborting")
            return 1

        print(f"[3/5] waiting {DISCOVERY_WAIT}s for UDP mesh to converge...")
        await asyncio.sleep(DISCOVERY_WAIT)

        print(f"[4/5] polling /api/peers on healthy instances...")
        healthy_instances = [inst for inst, ok in zip(instances, results) if ok]
        # Poll in batches to avoid too many concurrent connections
        peer_counts = []
        BATCH_POLL = 100
        for i in range(0, len(healthy_instances), BATCH_POLL):
            batch = healthy_instances[i:i + BATCH_POLL]
            counts = await asyncio.gather(*[_peers_count(inst["port"]) for inst in batch])
            peer_counts.extend(counts)
        valid = [c for c in peer_counts if c >= 0]
        if valid:
            avg = sum(valid) / len(valid)
            print(f"     peer count per node: min={min(valid)} avg={avg:.1f} max={max(valid)} (target>={healthy - 1})")
            full_aware = sum(1 for c in peer_counts if c >= healthy - 1)
            print(f"     nodes seeing >= {healthy - 1} peers: {full_aware}/{healthy} ({full_aware/healthy*100:.1f}%)")

        n_sample = min(SAMPLE_WORKFLOW, healthy)
        print(f"[5/5] sampled workflow on {n_sample} random healthy instances...")
        sample = random.sample(healthy_instances, n_sample)
        SEM = asyncio.Semaphore(15)
        async def _g(inst):
            async with SEM:
                return await _workflow(inst["port"], inst["idx"])
        t0 = time.time()
        wf = await asyncio.gather(*[_g(inst) for inst in sample], return_exceptions=True)
        print(f"     workflow batch done in {time.time() - t0:.1f}s")

        step_names = ["reg_a", "reg_b", "channel", "sock_a", "sock_b",
                      "join_a", "join_b", "chat_sent", "chat_received",
                      "call_init", "call_hang", "file_upload"]
        counts = {k: 0 for k in step_names}
        exc = 0
        for r in wf:
            if isinstance(r, Exception):
                exc += 1; continue
            for k in step_names:
                if r.get(k):
                    counts[k] += 1

        print()
        print("=============== RESULTS ===============")
        print(f"  spawn target        : {TARGET_N}")
        print(f"  spawn actual        : {spawned}  {'(aborted early)' if aborted_at else ''}")
        print(f"  healthy             : {healthy}/{spawned}")
        if valid:
            print(f"  mesh full-aware     : {full_aware}/{healthy} ({full_aware/healthy*100:.1f}%)")
        print(f"  workflow sample size: {n_sample}   exceptions={exc}")
        for k in step_names:
            pct = counts[k] / n_sample * 100
            tag = "PASS" if pct >= 95 else ("PARTIAL" if pct >= 50 else "FAIL")
            print(f"    {k:<16} : {counts[k]:3d}/{n_sample}  {pct:5.1f}%  {tag}")
        return 0

    finally:
        await _cleanup(instances)


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
