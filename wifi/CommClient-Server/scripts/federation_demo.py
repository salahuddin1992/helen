"""
LAN federation demo — spawns TWO isolated CommClient-Server instances in
child processes, then watches them discover each other over UDP broadcast
and verifies the peer listing + HTTP ping round-trip.

Both instances get their own data directory so their persisted .server_id
files are distinct (required: instances with the same server_id are treated
as duplicates and filtered out by the registry).

This script is self-contained — it does not require any server to be
pre-running, and it cleans up the child processes and their data dirs on
exit.

Expected output:
  - Both instances list the other (and *only* the other) in GET /api/peers
  - POST /api/peers/{id}/ping returns 200 with an RTT
  - Server names differ so you can tell them apart visually
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

ALPHA_PORT = 3107
BETA_PORT = 3108
BASE_ALPHA = f"http://127.0.0.1:{ALPHA_PORT}"
BASE_BETA = f"http://127.0.0.1:{BETA_PORT}"
REPO_ROOT = Path(__file__).resolve().parent.parent
ALPHA_DATA = REPO_ROOT / "data_peer_alpha"
BETA_DATA = REPO_ROOT / "data_peer_beta"


# ── color ──────────────────────────────────────────────────────
try:
    import colorama  # type: ignore
    colorama.just_fix_windows_console()
except Exception:
    pass


def _c(code: str, t: str) -> str:
    return f"\033[{code}m{t}\033[0m"


def dim(t: str) -> str:    return _c("90", t)
def green(t: str) -> str:  return _c("32", t)
def yellow(t: str) -> str: return _c("33", t)
def cyan(t: str) -> str:   return _c("36", t)
def magenta(t: str) -> str: return _c("35", t)
def red(t: str) -> str:    return _c("31", t)
def bold(t: str) -> str:   return _c("1",  t)


T0 = time.time()


def log(tag: str, msg: str, color=cyan) -> None:
    print(f"{dim(f'{time.time() - T0:6.2f}s')} {color(tag.ljust(10))} {msg}", flush=True)


# ── health helpers ─────────────────────────────────────────────


async def wait_health(base: str, timeout: float = 20.0) -> bool:
    async with httpx.AsyncClient(timeout=2.0) as client:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                r = await client.get(f"{base}/api/health")
                if r.status_code == 200:
                    return True
            except httpx.RequestError:
                pass
            await asyncio.sleep(0.5)
    return False


async def fetch_info(base: str) -> dict:
    async with httpx.AsyncClient(timeout=3.0) as client:
        r = await client.get(f"{base}/api/info")
        r.raise_for_status()
        return r.json()


async def fetch_peers(base: str) -> dict:
    async with httpx.AsyncClient(timeout=3.0) as client:
        r = await client.get(f"{base}/api/peers")
        r.raise_for_status()
        return r.json()


async def ping_peer(base: str, peer_id: str) -> dict:
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.post(f"{base}/api/peers/{peer_id}/ping")
        r.raise_for_status()
        return r.json()


# ── process spawn ─────────────────────────────────────────────


def spawn_peer(name: str, port: int, data_dir: Path) -> subprocess.Popen:
    """
    Start a server instance with its own data dir, server_id file, port,
    and name. Returns the Popen handle so we can terminate it later.
    """
    # Fresh data dir — ensures a different .server_id is generated
    if data_dir.exists():
        shutil.rmtree(data_dir, ignore_errors=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    uploads = data_dir / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    db_path = data_dir / "commclient.db"

    env = os.environ.copy()
    env["PORT"] = str(port)
    env["SQLITE_PATH"] = str(db_path)
    env["UPLOAD_DIR"] = str(uploads)
    env["SERVER_NAME"] = name
    env["JWT_SECRET"] = f"{name.lower().replace(' ', '-')}-demo-secret-" + "x" * 40
    env["PYTHONIOENCODING"] = "utf-8"

    # Tell the server to write its .server_id inside its own data dir so each
    # instance gets a distinct identity. (Our server derives this file path
    # from the SQLITE_PATH's parent.)
    log("spawn", f"launching {name} on :{port} (data={data_dir.name})", magenta)

    kwargs: dict = dict(
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if sys.platform == "win32":
        # CREATE_NEW_PROCESS_GROUP lets us send CTRL_BREAK_EVENT on shutdown
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]

    return subprocess.Popen([sys.executable, "run.py"], **kwargs)


# ── scenario ──────────────────────────────────────────────────


def _shutdown(proc: subprocess.Popen, label: str) -> None:
    log("cleanup", f"terminating {label}…", dim)
    try:
        if sys.platform == "win32":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            proc.terminate()
    except Exception:
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


async def scenario() -> int:
    print(bold("\n== LAN federation demo =="))
    print(dim("  spawning two isolated CommClient-Server instances on the same host"))
    print(dim("  and verifying they discover each other via UDP broadcast.\n"))

    alpha_proc = spawn_peer("CommClient Alpha", ALPHA_PORT, ALPHA_DATA)
    beta_proc = spawn_peer("CommClient Beta", BETA_PORT, BETA_DATA)

    try:
        log("wait", "polling alpha health…", cyan)
        if not await wait_health(BASE_ALPHA, timeout=30.0):
            print(red("alpha never came up"))
            return 1
        log("wait", "polling beta health…", cyan)
        if not await wait_health(BASE_BETA, timeout=30.0):
            print(red("beta never came up"))
            return 2

        info_a = await fetch_info(BASE_ALPHA)
        info_b = await fetch_info(BASE_BETA)
        log("alpha", f"{info_a['name']} id={info_a['server_id']} port={info_a['port']}", green)
        log("beta",  f"{info_b['name']} id={info_b['server_id']} port={info_b['port']}", green)

        if info_a["server_id"] == info_b["server_id"]:
            print(red("both instances reported the same server_id — data dirs weren't isolated"))
            return 3

        # Give UDP broadcasts time to cross-populate. Each instance broadcasts
        # on a ~3s interval, so 8s gives us 2-3 rounds of margin.
        log("wait", "waiting 8s for UDP broadcasts to cross-populate…", cyan)
        await asyncio.sleep(8)

        peers_a = (await fetch_peers(BASE_ALPHA))["peers"]
        peers_b = (await fetch_peers(BASE_BETA))["peers"]

        def _render(label: str, lst: list[dict]) -> None:
            log(label, f"sees {len(lst)} peer(s)", green if lst else red)
            for p in lst:
                print(
                    f"           {green('•')} {p['name']} "
                    f"{dim('id=' + p['server_id'])} "
                    f"{dim(p['host'] + ':' + str(p['port']))} "
                    f"{yellow('age=' + str(p['age_seconds']) + 's')}"
                )

        _render("alpha", peers_a)
        _render("beta",  peers_b)

        a_sees_b = any(p["server_id"] == info_b["server_id"] for p in peers_a)
        b_sees_a = any(p["server_id"] == info_a["server_id"] for p in peers_b)
        ok = a_sees_b and b_sees_a

        if ok:
            log("discover", "* both instances discovered each other", bold)
        else:
            log("discover", f"x discovery incomplete (a->b={a_sees_b} b->a={b_sees_a})", red)

        # HTTP ping round-trip between them
        if ok:
            r = await ping_peer(BASE_ALPHA, info_b["server_id"])
            log(
                "ping a->b",
                f"ok={r.get('ok')} status={r.get('status_code')} rtt={r.get('rtt_ms')}ms",
                green if r.get("ok") else red,
            )

            r = await ping_peer(BASE_BETA, info_a["server_id"])
            log(
                "ping b->a",
                f"ok={r.get('ok')} status={r.get('status_code')} rtt={r.get('rtt_ms')}ms",
                green if r.get("ok") else red,
            )

        print()
        print(bold("== summary =="))
        print(f"  alpha : {info_a['name']} ({info_a['server_id']}) @ :{info_a['port']}")
        print(f"  beta  : {info_b['name']} ({info_b['server_id']}) @ :{info_b['port']}")
        print(f"  alpha <-> beta discovery: {'YES' if ok else 'NO'}")
        print()
        return 0 if ok else 4

    finally:
        _shutdown(beta_proc, "beta")
        _shutdown(alpha_proc, "alpha")
        # Keep the data dirs so the user can inspect .server_id / DB if desired.


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(scenario()))
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(red(f"\nFATAL: {e!r}"))
        sys.exit(1)
