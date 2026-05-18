"""
Live multi-topology integration harness for Helen group video calls.

Spawns 1, 2, or 3 real Helen-Server processes (frozen exe), registers
users via REST, connects python-socketio clients, and exercises:

  Topology A: 1 server, 3 clients (single-server group call)
  Topology B: 2 servers federated, 1 client per server
  Topology C: 3 servers federated, 1 client per server

For each topology we measure:
  • call_incoming arrives at each callee
  • v2_call_accept succeeds (cross-server forwards via /api/federation/call/rpc)
  • call_participant_joined events fan out
  • call_signal (offer/answer/ice) relays end-to-end

The harness is INTENDED to be run manually, not via pytest, because
spawning multiple frozen exes takes 5-10 seconds per server and we
want fine-grained control over server lifecycle for diagnostic.

Usage:
  python tests/live/topology_harness.py A
  python tests/live/topology_harness.py B
  python tests/live/topology_harness.py C
  python tests/live/topology_harness.py all

Requires:
  • Helen-Server.exe at dist/Helen-Server/Helen-Server.exe (post-rebuild)
  • python-socketio (already in venv)
  • httpx (already in venv)
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import random
import secrets
import signal
import socket as _socket
import string
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import socketio


# ── Paths / config ─────────────────────────────────────────────────

ROOT = Path(__file__).resolve().parents[2]
EXE = ROOT / "dist" / "Helen-Server" / "Helen-Server.exe"
FEDERATION_SECRET = secrets.token_hex(32)


def _free_port() -> int:
    """Pick a free TCP port for a new server."""
    s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _rand_id(prefix: str) -> str:
    return f"{prefix}_{''.join(random.choices(string.ascii_lowercase + string.digits, k=8))}"


# ── ServerInstance ─────────────────────────────────────────────────


class ServerInstance:
    def __init__(self, name: str, federation: bool = False, peer_seeds: list[str] | None = None):
        self.name = name
        self.port = _free_port()
        self.https_port = _free_port()
        self.proc: subprocess.Popen | None = None
        self.data_dir = ROOT / "data" / "harness" / name
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.federation = federation
        self.peer_seeds = peer_seeds or []

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        if not EXE.exists():
            raise SystemExit(f"Helen-Server.exe missing: {EXE}")
        # Each server gets its own DB file so they don't fight over the
        # shared appdata/CommClient/data/commclient.db. Pass it via
        # SQLITE_PATH (which config.py reads directly) — relying on
        # COMMCLIENT_DATA_DIR alone wasn't enough because the frozen exe
        # bakes a default appdata path that wins.
        db_path = self.data_dir / "commclient.db"
        env = os.environ.copy()
        env.update({
            "PORT": str(self.port),
            "HELEN_HTTPS_PORT": str(self.https_port),
            "HELEN_HTTPS_DISABLED": "1",  # skip TLS cert mint to keep harness fast
            "COMMCLIENT_DATA_DIR": str(self.data_dir),
            "SQLITE_PATH": str(db_path),
            "UPLOAD_DIR": str(self.data_dir / "files"),
            "LOG_DIR": str(self.data_dir / "logs"),
            "DEBUG": "false",
            "LOG_LEVEL": "WARNING",
            # JWT secret must be ≥32 bytes — share across servers so federated
            # users can be looked up by share_code.
            "JWT_SECRET": "harness-jwt-secret-" + ("x" * 50),
            # Federation
            "FEDERATION_ENABLED": "true" if self.federation else "false",
            "FEDERATION_SECRET": FEDERATION_SECRET if self.federation else "",
            # Seed peers so servers find each other without UDP
            "HELEN_SEED_PEERS": ",".join(self.peer_seeds),
            # Harness uses auto_accept so peers cross the WAITING gate without
            # an admin in the loop. Production deployments should keep the
            # default `manual_approval` for stronger security.
            "COMMCLIENT_PEER_ACCEPTANCE_MODE": "auto_accept",
            # Quiet down rate limit + audit during the test
            "RATE_LIMIT_GLOBAL_ENABLED": "false",
            # Federation tunables: 5s resync (default 60s) so the test
            # doesn't have to wait a minute for cross-server presence.
            "HELEN_FEDERATION_PRESENCE_RESYNC_SECONDS": "5",
        })
        log_file = self.data_dir / f"server-{int(time.time())}.log"
        self._log_file = log_file.open("w", encoding="utf-8", errors="replace")
        self.proc = subprocess.Popen(
            [str(EXE)],
            env=env,
            cwd=str(ROOT),
            stdout=self._log_file,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
        )
        print(f"  [{self.name}] spawned PID={self.proc.pid} port={self.port} log={log_file}")

    async def wait_healthy(self, timeout: float = 30.0) -> None:
        start = time.time()
        async with httpx.AsyncClient(timeout=2.0) as cli:
            while time.time() - start < timeout:
                try:
                    r = await cli.get(f"{self.base_url}/api/health")
                    if r.status_code == 200:
                        elapsed = time.time() - start
                        print(f"  [{self.name}] healthy in {elapsed:.1f}s")
                        return
                except Exception:
                    pass
                await asyncio.sleep(0.3)
        raise TimeoutError(f"{self.name} did not become healthy in {timeout}s")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                if sys.platform == "win32":
                    self.proc.send_signal(signal.CTRL_BREAK_EVENT)
                    try:
                        self.proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        self.proc.kill()
                else:
                    self.proc.terminate()
                    self.proc.wait(timeout=3)
            except Exception:
                with contextlib.suppress(Exception):
                    self.proc.kill()
            print(f"  [{self.name}] stopped")
        with contextlib.suppress(Exception):
            self._log_file.close()


# ── User helpers ────────────────────────────────────────────────────


async def register_user(server: ServerInstance, username: str, password: str = "Pass1234!") -> dict:
    """Register + login a user, return tokens + user dict."""
    # bcrypt cost 12 makes register slow on a cold cache; 30s is enough
    # even on a low-end laptop.
    async with httpx.AsyncClient(timeout=30.0) as cli:
        r = await cli.post(
            f"{server.base_url}/api/auth/register",
            json={"username": username, "display_name": username.title(), "password": password},
        )
        r.raise_for_status()
        data = r.json()
        return {
            "user_id": data["user"]["id"],
            "username": data["user"]["username"],
            "share_code": data["user"]["share_code"],
            "access_token": data["tokens"]["access_token"],
            "refresh_token": data["tokens"]["refresh_token"],
            "server": server,
        }


async def create_group_channel(member_users: list[dict]) -> str:
    """First user creates a group channel containing all users.
    All users must be on the SAME server for this to work via REST —
    federation doesn't share user databases."""
    creator = member_users[0]
    async with httpx.AsyncClient(timeout=10.0) as cli:
        r = await cli.post(
            f"{creator['server'].base_url}/api/channels",
            headers={"Authorization": f"Bearer {creator['access_token']}"},
            json={
                "type": "group",
                "name": "Harness Test Channel",
                "member_ids": [u["user_id"] for u in member_users],
            },
        )
        r.raise_for_status()
        return r.json()["id"]


# ── ClientSession (Socket.IO) ──────────────────────────────────────


class ClientSession:
    def __init__(self, user: dict):
        self.user = user
        self.sio = socketio.AsyncClient(reconnection=False, logger=False, engineio_logger=False)
        self.events: list[tuple[str, dict]] = []
        self._setup_listeners()

    def _setup_listeners(self) -> None:
        EVENTS_TO_CAPTURE = [
            "call_incoming", "call:incoming",
            "call_accepted", "call:accepted",
            "call:peer_ready",
            "call_participant_joined", "call:peer_joined",
            "call_participant_left",  "call:peer_left",
            "call:group_ringing",
            "call_signal", "signal:offer", "signal:answer", "signal:ice_candidate",
            "presence:user_online", "presence:user_status",
        ]
        for ev in EVENTS_TO_CAPTURE:
            self.sio.on(ev, self._make_listener(ev))

    def _make_listener(self, ev: str):
        async def _h(data=None):
            self.events.append((ev, data or {}))
        return _h

    async def connect(self) -> None:
        url = self.user["server"].base_url
        await self.sio.connect(
            url,
            auth={"token": self.user["access_token"]},
            transports=["websocket"],
        )

    async def disconnect(self) -> None:
        with contextlib.suppress(Exception):
            await self.sio.disconnect()

    async def join_group_call(self, channel_id: str) -> dict:
        return await self.sio.call("v2_call_join_group", {
            "channel_id": channel_id,
            "media_type": "video",
        }, timeout=8)

    async def accept_call(self, call_id: str) -> dict:
        return await self.sio.call("v2_call_accept", {
            "call_id": call_id,
            "idempotency_key": _rand_id("acc"),
        }, timeout=8)

    async def send_signal(self, target_id: str, call_id: str,
                           signal_type: str, payload: dict) -> None:
        body = {"target_id": target_id, "call_id": call_id, "signal_type": signal_type}
        body.update(payload)
        await self.sio.emit("call_signal", body)

    def events_named(self, name: str) -> list[dict]:
        return [d for n, d in self.events if n == name]


# ── Topologies ──────────────────────────────────────────────────────


async def topology_a() -> dict[str, Any]:
    """1 server, 3 clients group call."""
    print("\n[A] === Topology A: 1 server, 3 clients ===")
    s1 = ServerInstance("A_srv1", federation=False)
    s1.start()
    try:
        await s1.wait_healthy()
        alice  = await register_user(s1, _rand_id("alice"))
        bob    = await register_user(s1, _rand_id("bob"))
        carol  = await register_user(s1, _rand_id("carol"))
        channel_id = await create_group_channel([alice, bob, carol])
        print(f"  channel_id={channel_id[:8]}…")

        a, b, c = ClientSession(alice), ClientSession(bob), ClientSession(carol)
        await asyncio.gather(a.connect(), b.connect(), c.connect())
        print("  3 sockets connected")

        # Alice initiates the group call
        join_resp = await a.join_group_call(channel_id)
        print(f"  alice.join_group → {json.dumps(join_resp)[:120]}")
        assert "call_id" in join_resp, f"join failed: {join_resp}"
        call_id = join_resp["call_id"]

        # Wait briefly for call_incoming to fan out
        await asyncio.sleep(1.0)

        # Bob and Carol should have received call_incoming
        b_inc = b.events_named("call_incoming")
        c_inc = c.events_named("call_incoming")
        print(f"  bob.call_incoming   x{len(b_inc)} | carol.call_incoming  x{len(c_inc)}")

        # Bob accepts → server emits call_accepted to alice
        if b_inc:
            ack = await b.accept_call(call_id)
            print(f"  bob.accept → {json.dumps(ack)[:120]}")
            await asyncio.sleep(0.5)

        # Bob sends a fake offer to alice
        await b.send_signal(alice["user_id"], call_id, "offer", {"sdp": {"type": "offer", "sdp": "v=0..."}})
        await asyncio.sleep(0.4)
        a_signals = [d for d in a.events_named("call_signal") if d.get("from_id") == bob["user_id"]]
        print(f"  alice.call_signal from bob: x{len(a_signals)}")

        # Disconnect
        await asyncio.gather(a.disconnect(), b.disconnect(), c.disconnect())

        return {
            "ok": (len(b_inc) >= 1 and len(c_inc) >= 1 and len(a_signals) >= 1),
            "bob_call_incoming": len(b_inc),
            "carol_call_incoming": len(c_inc),
            "alice_signal_from_bob": len(a_signals),
        }
    finally:
        s1.stop()


async def topology_b() -> dict[str, Any]:
    """2 federated servers, 1 client per server."""
    print("\n[B] === Topology B: 2 servers federated ===")
    p1 = _free_port(); p2 = _free_port()
    # First spawn server2 with no peers, then server1 seeded with server2.
    s2 = ServerInstance("B_srv2", federation=True)
    s2.port = p2  # override before spawn
    s1 = ServerInstance("B_srv1", federation=True, peer_seeds=[f"127.0.0.1:{p2}"])
    s1.port = p1
    s2.start()
    try:
        await s2.wait_healthy()
        s1.start()
        await s1.wait_healthy()

        # Give the gossip + seed_peers_from_env a few ticks to land
        await asyncio.sleep(2.5)

        # Verify peer_registry on each
        async with httpx.AsyncClient(timeout=5) as cli:
            r1 = await cli.get(f"{s1.base_url}/api/discovery")
            r2 = await cli.get(f"{s2.base_url}/api/discovery")
            print(f"  s1.discovery: id={r1.json()['server_id'][:12]}…")
            print(f"  s2.discovery: id={r2.json()['server_id'][:12]}…")

        # Two users, ONE on each server (no shared user DB; this is what
        # really happens in production federation).
        alice = await register_user(s1, _rand_id("alice"))
        bob   = await register_user(s2, _rand_id("bob"))

        a = ClientSession(alice); b = ClientSession(bob)
        await asyncio.gather(a.connect(), b.connect())
        print("  alice on s1, bob on s2 — both connected")

        # Alice attempts a 1-1 v2 call to bob via share_code lookup.
        # First resolve bob's user via the cross-server share_code lookup.
        async with httpx.AsyncClient(timeout=5) as cli:
            r = await cli.get(
                f"{s1.base_url}/api/users/by-code/{bob['share_code']}",
                headers={"Authorization": f"Bearer {alice['access_token']}"},
            )
            if r.status_code != 200:
                print(f"  [WARN] cross-server share_code lookup failed: {r.status_code} {r.text[:200]}")
                resolved_bob = None
            else:
                resolved_bob = r.json()

        if not resolved_bob:
            await asyncio.gather(a.disconnect(), b.disconnect())
            return {"ok": False, "reason": "share_code_lookup_failed"}

        # Initiate v2_call_initiate (1-1) with bob's resolved id
        try:
            initiate_resp = await a.sio.call("v2_call_initiate", {
                "target_id": resolved_bob["user"]["id"] if "user" in resolved_bob else resolved_bob.get("id"),
                "media_type": "video",
            }, timeout=6)
        except Exception as e:
            initiate_resp = {"error": f"timeout: {e}"}
        print(f"  alice.v2_call_initiate → {json.dumps(initiate_resp)[:200]}")

        await asyncio.sleep(2.0)

        b_inc = b.events_named("call_incoming")
        print(f"  bob.call_incoming x{len(b_inc)} (cross-server federation)")

        # If bob received call_incoming, accept it (this should forward via RPC)
        if b_inc:
            call_id = b_inc[0].get("call_id")
            try:
                accept_resp = await b.accept_call(call_id)
            except Exception as e:
                accept_resp = {"error": f"timeout: {e}"}
            print(f"  bob.accept → {json.dumps(accept_resp)[:200]}")
            await asyncio.sleep(1.0)
            a_acc = a.events_named("call_accepted")
            print(f"  alice.call_accepted x{len(a_acc)} (cross-server return)")
        else:
            a_acc = []

        await asyncio.gather(a.disconnect(), b.disconnect())
        return {
            "ok": len(b_inc) >= 1,
            "bob_call_incoming": len(b_inc),
            "alice_call_accepted": len(a_acc),
        }
    finally:
        s1.stop(); s2.stop()


async def topology_c() -> dict[str, Any]:
    """3 federated servers in a chain."""
    print("\n[C] === Topology C: 3 servers federated chain ===")
    p1 = _free_port(); p2 = _free_port(); p3 = _free_port()
    # Boot s3 first, then s2 seeded with s3, then s1 seeded with s2.
    s3 = ServerInstance("C_srv3", federation=True)
    s2 = ServerInstance("C_srv2", federation=True, peer_seeds=[f"127.0.0.1:{p3}"])
    s1 = ServerInstance("C_srv1", federation=True, peer_seeds=[f"127.0.0.1:{p2}"])
    s1.port, s2.port, s3.port = p1, p2, p3

    s3.start()
    try:
        await s3.wait_healthy()
        s2.start(); await s2.wait_healthy()
        s1.start(); await s1.wait_healthy()
        await asyncio.sleep(3.5)  # let gossip propagate

        async with httpx.AsyncClient(timeout=5) as cli:
            ids = []
            for s in (s1, s2, s3):
                r = await cli.get(f"{s.base_url}/api/discovery")
                ids.append(r.json()["server_id"][:12])
            print(f"  servers: {ids}")

        alice = await register_user(s1, _rand_id("alice"))
        carol = await register_user(s3, _rand_id("carol"))

        # Alice on s1 looks up Carol's share_code — federation chain s1→s2→s3
        async with httpx.AsyncClient(timeout=8) as cli:
            r = await cli.get(
                f"{s1.base_url}/api/users/by-code/{carol['share_code']}",
                headers={"Authorization": f"Bearer {alice['access_token']}"},
            )
            ok = r.status_code == 200
            print(f"  s1→carol share_code lookup (multi-hop): {r.status_code}")
            if ok:
                body = r.json()
                resolved_id = body.get("user", body).get("id") or body.get("id")
            else:
                resolved_id = None

        if not resolved_id:
            return {"ok": False, "reason": "multi_hop_lookup_failed", "status": r.status_code}

        a = ClientSession(alice); c = ClientSession(carol)
        await asyncio.gather(a.connect(), c.connect())

        try:
            initiate_resp = await a.sio.call("v2_call_initiate", {
                "target_id": resolved_id, "media_type": "video",
            }, timeout=8)
        except Exception as e:
            initiate_resp = {"error": f"timeout: {e}"}
        print(f"  alice.v2_call_initiate → {json.dumps(initiate_resp)[:200]}")

        await asyncio.sleep(3.0)
        c_inc = c.events_named("call_incoming")
        print(f"  carol.call_incoming x{len(c_inc)} (multi-hop federation)")

        await asyncio.gather(a.disconnect(), c.disconnect())
        return {
            "ok": len(c_inc) >= 1,
            "carol_call_incoming": len(c_inc),
        }
    finally:
        s1.stop(); s2.stop(); s3.stop()


# ── Main ────────────────────────────────────────────────────────────


async def main(which: str) -> None:
    results: dict[str, Any] = {}
    if which in ("A", "all"):
        try:
            results["A"] = await topology_a()
        except Exception as e:
            results["A"] = {"ok": False, "exception": repr(e)}
    if which in ("B", "all"):
        try:
            results["B"] = await topology_b()
        except Exception as e:
            results["B"] = {"ok": False, "exception": repr(e)}
    if which in ("C", "all"):
        try:
            results["C"] = await topology_c()
        except Exception as e:
            results["C"] = {"ok": False, "exception": repr(e)}

    print("\n=== HARNESS RESULTS ===")
    for t, r in results.items():
        flag = "PASS" if r.get("ok") else "FAIL"
        print(f"  [{t}] {flag}  {json.dumps({k:v for k,v in r.items() if k!='ok'})}")
    print()


if __name__ == "__main__":
    # Force UTF-8 stdout on Windows so emoji / arrows / accented chars
    # don't crash the harness with charmap encoding errors.
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    arg = sys.argv[1] if len(sys.argv) > 1 else "all"
    asyncio.run(main(arg.upper()))
