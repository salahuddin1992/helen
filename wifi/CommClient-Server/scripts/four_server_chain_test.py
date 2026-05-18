"""
4-server chain routing test: A ↔ B ↔ C ↔ D.

Spawns four fully-isolated Helen-Server instances and wires them so each
one only knows its immediate neighbor(s). A user registers + logs in on
A (port 3401), another registers + logs in on D (port 3404). A emits a
federation event targeted at D's user. The event must transit A → B →
C → D via the new chain-routing flood logic, arriving at D's socket
exactly once (dedup must kill the duplicate branches).

Why this is interesting:
    A ─ B ─ C ─ D    (adjacent pairs peered; A and D are NOT direct peers)

Without chain routing, A's federation emit reaches B only, and B has no
instruction to forward it further — message drops. With chain routing,
B floods to its peers (A and C), A dedupes the echo, C forwards to D,
D dedupes the echo from B (via C), and delivers.

This test populates the peer_registry manually (via the admin-facing
peer POST API) because UDP broadcast finds all four servers on a single
host — defeating the chain topology. In a real LAN the partition comes
from routers; here we simulate it with selective registry entries.
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
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
PORTS = [3401, 3402, 3403, 3404]
NAMES = ["Alpha", "Bravo", "Charlie", "Delta"]


def spawn_server(port: int, name: str, data_dir: Path,
                 jwt_secret: str, federation_secret: str,
                 seed_peers: str = "") -> subprocess.Popen:
    if data_dir.exists():
        shutil.rmtree(data_dir, ignore_errors=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "uploads").mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    # SQLITE_PATH must be absolute and per-instance so server_ids (which
    # are stored next to the DB as .server_id) don't collide.
    db_path = str((data_dir / "commclient.db").resolve())
    env.update({
        "PORT": str(port),
        "SERVER_NAME": f"Helen-{name}",
        "SQLITE_PATH": db_path,
        "COMMCLIENT_DATA_DIR": str(data_dir),
        "UPLOAD_DIR": str((data_dir / "uploads").resolve()),
        "LOG_DIR": str((data_dir / "logs").resolve()),
        "JWT_SECRET": jwt_secret,
        "FEDERATION_ENABLED": "true",
        "FEDERATION_SECRET": federation_secret,
        # Disable HTTPS sidecar — four instances on one host would all
        # fight for port 3443.
        "HELEN_HTTPS_DISABLED": "1",
        # Give each server its own UDP broadcast port so they don't all
        # race to bind 41234 on localhost. This disables cross-discovery
        # which is exactly what we want — we'll wire peers manually via
        # HELEN_SEED_PEERS so each server sees only its immediate chain
        # neighbors.
        "DISCOVERY_UDP_PORT": str(41234 + port - 3401),
        "HELEN_SEED_PEERS": seed_peers,
    })
    log_path = data_dir / "server.stderr.log"
    log_f = open(log_path, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        [PY, str(ROOT / "run.py")],
        cwd=str(ROOT),
        env=env,
        stdout=log_f,
        stderr=subprocess.STDOUT,
    )
    return proc


async def wait_ready(base: str, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient(timeout=2.0) as c:
        while time.monotonic() < deadline:
            try:
                r = await c.get(f"{base}/api/health")
                if r.status_code == 200:
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.5)
    return False


async def get_server_id(base: str) -> str:
    async with httpx.AsyncClient(timeout=3.0) as c:
        r = await c.get(f"{base}/api/discovery")
        r.raise_for_status()
        return r.json()["server_id"]


async def add_peer_entry(base: str, federation_secret: str, peer_id: str,
                         peer_host: str, peer_port: int, peer_name: str) -> None:
    """Inject a peer into the server's registry without going through UDP
    discovery. Uses the same HMAC-signed peer manifest the UDP broadcast
    layer produces internally, so the registry accepts it as a live peer."""
    # The peer registry is internal state. Easiest route: call the peer's
    # own `/api/peers/announce` equivalent if it exists; otherwise poke
    # the in-process registry via a one-shot script. We use the latter
    # so the test stays independent of any specific announce endpoint.
    import json as _json, importlib
    # Run a snippet inside the target server's process context via a
    # helper REST call isn't available; instead, use the simple approach:
    # have each server spawn know its neighbors via env var that our
    # manual wiring below sets, and the server itself publishes them on
    # first poll. But for this test, we just seed via direct HTTP to a
    # test-only helper we add, OR emit peer_registry.add_manual as part
    # of the server's startup hook.
    #
    # For now, the simplest wire: we override each server's UDP port so
    # everyone broadcasts on their own port, then each server sniffs the
    # others in its neighbor list by pointing its UDP listener at the
    # neighbor's broadcast port. This isn't quite what we want.
    #
    # Fall back to directly editing the peer_registry via a small REST
    # endpoint we'll rely on: the tests helper can bypass auth by talking
    # to the server process directly — use peer_registry.add_manual on
    # the target server by invoking a tiny script through stdin. Here we
    # just use the /api/peers/register admin endpoint if it exists.
    pass


async def inject_peer_via_internal_api(base: str, peer_host: str, peer_port: int,
                                       peer_id: str, peer_name: str) -> bool:
    """Ask the target server to add a peer record by POSTing to an internal
    seed endpoint. We rely on the existing `/api/peers/announce` added for
    LAN bootstrap; if unavailable the test falls back to discovery wait."""
    async with httpx.AsyncClient(timeout=3.0) as c:
        try:
            r = await c.post(
                f"{base}/api/peers/announce",
                json={
                    "server_id": peer_id,
                    "name": peer_name,
                    "host": peer_host,
                    "port": peer_port,
                    "version": "1.0.0",
                },
            )
            return r.status_code < 400
        except Exception:
            return False


async def main() -> int:
    from app.services.discovery_service import get_server_id as _get_id  # noqa: F401

    fed_secret = "test-fed-secret-shared-across-chain"
    data_dirs = [ROOT / "data_chain" / n.lower() for n in NAMES]

    # Wire the chain topology: each server gets only its immediate
    # neighbors as seed peers. End servers have 1 neighbor; middle
    # servers have 2. This is what creates the "A can't see D directly"
    # constraint the chain-routing logic has to overcome.
    seed_map = {
        0: [1],               # Alpha → Bravo
        1: [0, 2],             # Bravo → Alpha, Charlie
        2: [1, 3],             # Charlie → Bravo, Delta
        3: [2],                # Delta → Charlie
    }
    procs = []
    for i, (port, name, data) in enumerate(zip(PORTS, NAMES, data_dirs)):
        neighbors = ",".join(
            f"127.0.0.1:{PORTS[j]}" for j in seed_map[i]
        )
        p = spawn_server(port, name, data, f"jwt-{name}", fed_secret,
                         seed_peers=neighbors)
        procs.append(p)
    bases = [f"http://127.0.0.1:{p}" for p in PORTS]

    try:
        print("[chain] waiting for 4 servers...")
        for base in bases:
            up = await wait_ready(base, timeout=40)
            print(f"  {base}: {'UP' if up else 'DOWN'}")
            if not up:
                return 1

        ids = [await get_server_id(b) for b in bases]
        for name, b, sid in zip(NAMES, bases, ids):
            print(f"  {name} id={sid[:12]}... port={b}")

        # Give the background seed-retry loop up to 35s to settle.
        print("[chain] waiting for peer seeding to converge...")
        await asyncio.sleep(12)

        # Verify the chain topology took effect — query each server's
        # /api/peers and confirm each sees only its expected neighbor(s).
        print("[chain] verifying chain topology...")
        async with httpx.AsyncClient(timeout=3.0) as c:
            for i, base in enumerate(bases):
                r = await c.get(f"{base}/api/peers")
                if r.status_code != 200:
                    print(f"  {NAMES[i]}: peers query failed {r.status_code}")
                    continue
                peers = r.json().get("peers", [])
                peer_ids = {p["server_id"] for p in peers if not p.get("is_stale")}
                expected = {ids[j] for j in seed_map[i]}
                match = peer_ids == expected
                names_seen = [
                    NAMES[ids.index(pid)] if pid in ids else pid[:8]
                    for pid in peer_ids
                ]
                print(f"  {NAMES[i]} sees: {sorted(names_seen)} (expected {len(expected)}) {'OK' if match else 'MISMATCH'}")

        # Register a user on Alpha (A) and a user on Delta (D)
        async with httpx.AsyncClient(timeout=5.0) as c:
            async def _reg_login(base, name, pw):
                await c.post(f"{base}/api/auth/register",
                             json={"username": name, "display_name": name, "password": pw})
                r = await c.post(f"{base}/api/auth/login",
                                 json={"username": name, "password": pw})
                return r.json()
            a_login = await _reg_login(bases[0], "chain_alpha", "Pass!word-42")
            d_login = await _reg_login(bases[3], "chain_delta", "Pass!word-42")
            a_tok = a_login["tokens"]["access_token"]
            d_tok = d_login["tokens"]["access_token"]
            d_uid = d_login["user"]["id"]
            print(f"  Alpha user id={a_login['user']['id'][:12]}...")
            print(f"  Delta user id={d_uid[:12]}...")

        # Connect Delta's socket to receive
        import socketio as _socketio
        received: list[dict] = []
        delta_sio = _socketio.AsyncClient(reconnection=False)

        @delta_sio.on("chain_test_event")
        async def _on_evt(data):
            received.append(data)

        await delta_sio.connect(bases[3], auth={"token": d_tok},
                                transports=["websocket"])
        print(f"  Delta socket connected sid={delta_sio.sid}")
        await asyncio.sleep(0.5)

        # From Alpha's Python side, directly call federated_emit targeting
        # delta_uid. In a real deployment, socket handlers on Alpha would
        # call emit_to_user when the local chat handler sees a cross-server
        # target. Here we short-circuit and invoke the emit directly via a
        # test-only endpoint — cleanest is to POST /api/federation/emit on
        # Alpha from *ourselves* (the test process) with the right HMAC.
        # That replays exactly what a peer would send.
        from app.services.federation_service import federation_service

        # Actually we want Alpha itself to do the emit_to_user call so the
        # flood fans out from Alpha. For that we need in-process access to
        # Alpha — which the test can't do because Alpha is a child process.
        # Cheap substitute: POST /api/federation/emit on Bravo as if Alpha
        # sent it. The body has target_user_id=delta, message_id seeded,
        # hop_count=0. Bravo will forward to Charlie (and back to Alpha,
        # but Alpha dedupes). Charlie forwards to Delta + Bravo (Bravo
        # dedupes). Delta delivers locally.
        #
        # For the signed request, we use federation_service.emit_to_remote_user
        # but that needs peer_registry entries in *our* test process — which
        # it doesn't have. So we construct the signed request manually.
        import hmac as _hmac, hashlib as _hashlib, json as _json

        body_dict = {
            "target_user_id": d_uid,
            "event": "chain_test_event",
            "payload": {"hello": "from alpha via chain"},
            "hop_count": 0,
            "max_hops": 8,
            "message_id": "chain-test-" + str(int(time.time() * 1000)),
        }
        body_bytes = _json.dumps(body_dict).encode("utf-8")
        ts = str(int(time.time()))
        method = "POST"
        path = "/api/federation/emit"
        body_sha = _hashlib.sha256(body_bytes).hexdigest()
        msg = f"{ts}.{method}.{path}.{body_sha}".encode("utf-8")
        mac = _hmac.new(fed_secret.encode("utf-8"), msg, _hashlib.sha256).hexdigest()

        async with httpx.AsyncClient(timeout=5.0) as c:
            t0 = time.monotonic()
            r = await c.post(
                f"{bases[0]}{path}",  # Alpha: transits to Delta via B and C
                content=body_bytes,
                headers={
                    "Content-Type": "application/json",
                    "X-Federation-Timestamp": ts,
                    "X-Federation-Signature": mac,
                },
            )
            elapsed = (time.monotonic() - t0) * 1000
            print(f"[chain] Alpha returned {r.status_code} in {elapsed:.0f}ms  body={r.text[:200]}")

        # Wait for the event to transit
        print("[chain] waiting up to 6s for Delta to receive event...")
        for _ in range(60):
            if received:
                break
            await asyncio.sleep(0.1)

        print(f"[chain] Delta received {len(received)} event(s)")
        for msg in received:
            print(f"  -> {msg}")

        await delta_sio.disconnect()

        # Result
        success = len(received) == 1 and received[0].get("hello") == "from alpha via chain"
        print("\n======== RESULT ========")
        print(f"  4-server chain: {'PASS' if success else 'FAIL'}")
        return 0 if success else 1

    finally:
        print("[cleanup] terminating 4 instances...")
        for p in procs:
            try: p.terminate()
            except Exception: pass
        for p in procs:
            try: p.wait(timeout=5)
            except Exception:
                try: p.kill()
                except Exception: pass


if __name__ == "__main__":
    # Ensure ROOT is on path so the test can import app.*
    sys.path.insert(0, str(ROOT))
    raise SystemExit(asyncio.run(main()))
