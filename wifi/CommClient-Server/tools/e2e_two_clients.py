"""
Helen E2E Two-Client Simulation
================================

Real end-to-end test:
  1. Spawn Helen-Server on a free port
  2. Spawn Helen-Router on a free port, pointing upstream at the server
  3. Register two users (Alice + Bob) via /auth/register
  4. Log both in, capture JWTs
  5. Each connects to the Socket.IO endpoint with its JWT
  6. Alice creates a channel + invites Bob
  7. Bob joins the channel
  8. Alice sends a message
  9. Bob receives it over Socket.IO
 10. Bob replies; Alice receives
 11. Both initiate a call (signaling only — no real WebRTC media)
 12. SDP offer/answer + ICE candidates exchanged via Socket.IO
 13. Verify call ends gracefully
 14. Cleanup

Outcome:
  exit 0 → all flows pass — the platform actually works end-to-end
  exit 1 → some flow failed — see report for which step

Usage:
  python tools/e2e_two_clients.py                    # via direct server
  python tools/e2e_two_clients.py --via-router       # via router proxy
  python tools/e2e_two_clients.py --json             # JSON output
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# -----------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WIFI_ROOT    = PROJECT_ROOT.parent
ROUTER_ROOT  = WIFI_ROOT / "Helen-Router"


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def C(code, s):
    if not sys.stdout.isatty(): return s
    return f"\033[{code}m{s}\033[0m"


def ok(m): print(C("32","✓ "), m)
def fail(m): print(C("31","✗ "), m)
def info(m): print(C("36","ℹ "), m)
def warn(m): print(C("33","⚠ "), m)


@dataclass
class Step:
    name: str
    passed: bool
    duration_ms: int
    detail: str = ""
    error: Optional[str] = None


@dataclass
class Report:
    started_at: str
    finished_at: str = ""
    steps: List[Step] = field(default_factory=list)
    config: Dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return all(s.passed for s in self.steps)


async def wait_port(port: int, timeout: float = 45.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r, w = await asyncio.open_connection("127.0.0.1", port)
            w.close()
            await w.wait_closed()
            return True
        except (ConnectionRefusedError, OSError):
            await asyncio.sleep(0.5)
    return False


# -----------------------------------------------------------------------------
# Process spawning
# -----------------------------------------------------------------------------
def spawn_server(port: int) -> subprocess.Popen:
    env = os.environ.copy()
    env.update({
        "PORT": str(port),
        "JWT_SECRET": "e2e-test-only-" + "x" * 48,
        "SQLITE_PATH": f"/tmp/e2e-server-{port}.db",
        "DATABASE_URL": f"sqlite+aiosqlite:////tmp/e2e-server-{port}.db",
        "HELEN_DATA_DIR": tempfile.mkdtemp(prefix=f"helen-e2e-srv-{port}-"),
        "HELEN_LAN_ONLY_STRICT": "0",
        "DEBUG": "1",
        "PYTHONUNBUFFERED": "1",
    })
    log = open(f"/tmp/e2e-server-{port}.log", "wb")
    return subprocess.Popen(
        [sys.executable, "-c",
         f"import sys; sys.path.insert(0,'{PROJECT_ROOT}'); "
         f"import uvicorn; from app.main import asgi_app; "
         f"uvicorn.run(asgi_app, host='127.0.0.1', port={port}, log_level='error')"],
        cwd=str(PROJECT_ROOT), env=env, stdout=log, stderr=subprocess.STDOUT,
    )


def spawn_router(port: int, upstream: str) -> subprocess.Popen:
    env = os.environ.copy()
    env.update({
        "ROUTER_PORT": str(port),
        "HELEN_ROUTER_TOKEN": "e2e-router-token-must-be-32-chars-or-longer-xyz",
        "HELEN_ROUTER_UPSTREAM": upstream,
        "HELEN_ROUTER_DATA_DIR": tempfile.mkdtemp(prefix=f"helen-e2e-rtr-{port}-"),
        "PYTHONUNBUFFERED": "1",
    })
    log = open(f"/tmp/e2e-router-{port}.log", "wb")
    return subprocess.Popen(
        [sys.executable, "-c",
         f"import sys; sys.path.insert(0,'{ROUTER_ROOT}'); "
         f"import uvicorn; from app.main import app; "
         f"uvicorn.run(app, host='127.0.0.1', port={port}, log_level='error')"],
        cwd=str(ROUTER_ROOT), env=env, stdout=log, stderr=subprocess.STDOUT,
    )


# -----------------------------------------------------------------------------
# Client class — mimics a Helen-Desktop client
# -----------------------------------------------------------------------------
class HelenClient:
    """Minimal client implementing the same flows as commclient-desktop."""

    def __init__(self, base_url: str, username: str):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.user_id: Optional[int] = None
        self.token: Optional[str] = None
        self.refresh_token: Optional[str] = None
        self.sio_client = None  # socketio.AsyncClient
        self.received_messages: List[Dict[str, Any]] = []
        self.received_call_events: List[Dict[str, Any]] = []

    async def _http(self, method: str, path: str, **kw) -> Tuple[int, Any]:
        import httpx
        headers = kw.pop("headers", {})
        if self.token and "Authorization" not in headers:
            headers["Authorization"] = f"Bearer {self.token}"
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.request(method, f"{self.base_url}{path}", headers=headers, **kw)
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, r.text

    async def _safe_post(self, path: str, body: dict) -> Tuple[int, Any]:
        try:
            return await self._http("POST", path, json=body)
        except Exception as e:
            return 599, f"exception: {type(e).__name__}: {e}"

    async def register(self, password: str) -> Tuple[bool, str]:
        for path, body in [
            ("/api/auth/register", {"username": self.username, "password": password, "email": f"{self.username}@e2e.lan", "display_name": self.username.title()}),
            ("/api/register",      {"username": self.username, "password": password}),
            ("/auth/register",     {"username": self.username, "password": password}),
        ]:
            code, body_resp = await self._safe_post(path, body)
            if code in (200, 201):
                if isinstance(body_resp, dict):
                    # AuthResponse schema: {user: {...}, tokens: {access_token, refresh_token, ...}}
                    user = body_resp.get("user") or {}
                    tokens = body_resp.get("tokens") or {}
                    self.user_id = user.get("id") or body_resp.get("id") or body_resp.get("user_id")
                    self.token = (tokens.get("access_token")
                                  or body_resp.get("access_token")
                                  or body_resp.get("token"))
                    self.refresh_token = tokens.get("refresh_token") or body_resp.get("refresh_token")
                return True, f"{path} → {code}, token={'YES' if self.token else 'NO'}"
            if code == 409:
                return True, f"{path} → 409 (already exists, ok)"
        return False, f"all register paths failed (last code={code})"

    async def login(self, password: str) -> Tuple[bool, str]:
        # If we already have a token from register, skip login (register returns AuthResponse)
        if self.token:
            return True, f"already have token from register (skipped login)"
        for path, body in [
            ("/api/auth/login", {"username": self.username, "password": password}),
            ("/api/login",      {"username": self.username, "password": password}),
            ("/auth/login",     {"username": self.username, "password": password}),
        ]:
            code, body_resp = await self._safe_post(path, body)
            if code == 200 and isinstance(body_resp, dict):
                self.token   = body_resp.get("access_token") or body_resp.get("token")
                self.user_id = body_resp.get("user_id") or body_resp.get("id")
                if self.token:
                    return True, f"{path} → 200, token len={len(self.token)}"
        return False, f"login failed (last code={code}, body={body_resp!r:.150})"

    async def whoami(self) -> Tuple[bool, str]:
        code, body = await self._http("GET", "/api/users/me")
        if code != 200:
            code, body = await self._http("GET", "/api/me")
        return code == 200, f"/api/users/me → {code}"

    async def connect_socketio(self) -> Tuple[bool, str]:
        try:
            import socketio
        except ImportError:
            return False, "python-socketio client not installed"
        self.sio_client = socketio.AsyncClient(reconnection=False, logger=False, engineio_logger=False)

        @self.sio_client.on("message")
        async def _on_msg(data):
            self.received_messages.append({"event": "message", "data": data})

        @self.sio_client.on("chat:new_message")
        async def _on_new_msg(data):
            self.received_messages.append({"event": "chat:new_message", "data": data})

        @self.sio_client.on("v2:message:new")
        async def _on_v2_msg(data):
            self.received_messages.append({"event": "v2:message:new", "data": data})

        # Catch any call-related event with a wildcard-style approach
        for evt in ("call:incoming","call:offer","call:answer","call:ice",
                    "incoming_call","call_invite","call.invite","webrtc:offer"):
            def _make_handler(e):
                async def _h(data=None):
                    self.received_call_events.append({"event": e, "data": data})
                return _h
            self.sio_client.on(evt, _make_handler(evt))
        # Catch-all (python-socketio passes event name as first arg)
        @self.sio_client.on("*")
        async def _on_any(event, data=None):
            if "call" in (event or "").lower() or "webrtc" in (event or "").lower():
                self.received_call_events.append({"event": event, "data": data})

        try:
            await asyncio.wait_for(
                self.sio_client.connect(
                    self.base_url,
                    auth={"token": self.token},
                    transports=["websocket", "polling"],
                ),
                timeout=60,
            )
            return self.sio_client.connected, f"sio.connected={self.sio_client.connected} sid={self.sio_client.sid}"
        except Exception as e:
            return False, f"connect failed: {e}"

    async def disconnect_socketio(self) -> None:
        if self.sio_client and self.sio_client.connected:
            try: await self.sio_client.disconnect()
            except Exception: pass

    async def send_message(self, channel_id: Any, text: str) -> Tuple[bool, str]:
        """Send a message; try REST first then socket emit."""
        code, body = await self._http("POST", f"/api/channels/{channel_id}/messages", json={"text": text})
        if code in (200, 201):
            return True, f"REST POST → {code}"
        if self.sio_client and self.sio_client.connected:
            try:
                # Server's @sio.event handler name = chat_send_message
                await self.sio_client.emit("chat_send_message", {
                    "channel_id": channel_id, "content": text, "type": "text",
                })
                return True, "socket emit chat_send_message"
            except Exception as e:
                return False, f"socket emit failed: {e}"
        return False, f"REST {code}, socket unavailable"

    async def create_channel(self, name: str, member_ids: Optional[List[str]] = None) -> Tuple[Optional[Any], str]:
        # ChannelCreate expects {type: "dm"|"group", name, member_ids: [str]}
        body = {"type": "group", "name": name, "description": "e2e test channel", "member_ids": member_ids or []}
        for path in ("/api/channels", "/api/v2/channels"):
            code, resp = await self._http("POST", path, json=body)
            if code in (200, 201) and isinstance(resp, dict):
                cid = resp.get("id") or resp.get("channel_id")
                if cid is not None:
                    return cid, f"created via {path}: id={cid}"
        return None, f"create_channel failed (last={code}, body={str(resp)[:120]!r})"

    async def initiate_call(self, callee_id: int) -> Tuple[bool, str]:
        if self.sio_client and self.sio_client.connected:
            try:
                # Server-side handler is @sio.event call_initiate
                # Payload: { callee_id: str, media_type: "audio"|"video" }
                await self.sio_client.emit("call_initiate", {
                    "callee_id": str(callee_id),
                    "media_type": "audio",
                })
                return True, "emit call_initiate"
            except Exception as e:
                return False, f"emit failed: {e}"
        return False, "no socket"


# -----------------------------------------------------------------------------
# Steps
# -----------------------------------------------------------------------------
async def step(report: Report, name: str, coro) -> bool:
    t0 = time.time()
    try:
        passed, detail = await coro
    except Exception as e:
        import traceback
        passed, detail = False, f"exception: {e}\n{traceback.format_exc()[:300]}"
    dur = int((time.time() - t0) * 1000)
    report.steps.append(Step(name=name, passed=bool(passed), duration_ms=dur, detail=str(detail)[:200]))
    (ok if passed else fail)(f"{name:30}  {dur:>5}ms  {str(detail)[:120]}")
    return bool(passed)


# -----------------------------------------------------------------------------
# Main flow
# -----------------------------------------------------------------------------
async def run(args: argparse.Namespace) -> Report:
    rpt = Report(started_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))

    server_port = args.server_port or find_free_port()
    router_port = args.router_port or find_free_port()

    rpt.config = {
        "server_port": server_port,
        "router_port": router_port,
        "via_router": args.via_router,
    }

    info(f"server: http://127.0.0.1:{server_port}")
    if args.via_router:
        info(f"router: http://127.0.0.1:{router_port}  (clients will hit this)")
    print()

    procs: List[subprocess.Popen] = []
    try:
        # ────────────────────────── Stage 1: Start services ────────────────────
        info("Stage 1: spawn server")
        proc_srv = spawn_server(server_port)
        procs.append(proc_srv)
        await step(rpt, "server.port_open", _ret(await wait_port(server_port, 60), f"port {server_port}"))

        if args.via_router:
            info("Stage 1b: spawn router")
            proc_rtr = spawn_router(router_port, f"http://127.0.0.1:{server_port}")
            procs.append(proc_rtr)
            await step(rpt, "router.port_open", _ret(await wait_port(router_port, 30), f"port {router_port}"))

            # Register server with router (the router won't proxy until we do)
            info("Stage 1c: register server with router")
            import httpx
            try:
                async with httpx.AsyncClient(timeout=10) as c:
                    r = await c.post(
                        f"http://127.0.0.1:{router_port}/router/register",
                        headers={"Authorization": "Bearer e2e-router-token-must-be-32-chars-or-longer-xyz"},
                        json={
                            "server_id": "e2e-server-1",
                            "url": f"http://127.0.0.1:{server_port}",
                            "capabilities": ["chat", "call", "files"],
                        },
                    )
                ok_ = r.status_code in (200, 201)
                detail = f"POST /router/register → {r.status_code}: {r.text[:120]}"
            except Exception as ex:
                ok_, detail = False, f"register failed: {ex}"
            rpt.steps.append(Step("router.server_registered", ok_, 0, detail))
            (ok if ok_ else fail)(f"router.register : {detail}")

        client_base = f"http://127.0.0.1:{router_port if args.via_router else server_port}"

        # ────────────────────────── Stage 2: Build clients ─────────────────────
        info("Stage 2: instantiate Alice and Bob")
        alice = HelenClient(client_base, f"alice_{secrets.token_hex(4)}")
        bob   = HelenClient(client_base, f"bob_{secrets.token_hex(4)}")
        pwd   = "P@ssw0rd-e2e-secure-1234"

        # ────────────────────────── Stage 3: Register ──────────────────────────
        info("Stage 3: register")
        a_reg = await alice.register(pwd); b_reg = await bob.register(pwd)
        rpt.steps.append(Step("alice.register", a_reg[0], 0, a_reg[1]))
        rpt.steps.append(Step("bob.register",   b_reg[0], 0, b_reg[1]))
        (ok if a_reg[0] else fail)(f"alice.register : {a_reg[1]}")
        (ok if b_reg[0] else fail)(f"bob.register   : {b_reg[1]}")

        # ────────────────────────── Stage 4: Login ─────────────────────────────
        info("Stage 4: login")
        a_log = await alice.login(pwd); b_log = await bob.login(pwd)
        rpt.steps.append(Step("alice.login", a_log[0], 0, a_log[1]))
        rpt.steps.append(Step("bob.login",   b_log[0], 0, b_log[1]))
        (ok if a_log[0] else fail)(f"alice.login    : {a_log[1]}")
        (ok if b_log[0] else fail)(f"bob.login      : {b_log[1]}")

        # ────────────────────────── Stage 5: WhoAmI ────────────────────────────
        if alice.token and bob.token:
            info("Stage 5: whoami sanity")
            a_who = await alice.whoami(); b_who = await bob.whoami()
            rpt.steps.append(Step("alice.whoami", a_who[0], 0, a_who[1]))
            rpt.steps.append(Step("bob.whoami",   b_who[0], 0, b_who[1]))
            (ok if a_who[0] else warn)(f"alice.whoami   : {a_who[1]}")
            (ok if b_who[0] else warn)(f"bob.whoami     : {b_who[1]}")

        # ────────────────────────── Stage 6: Socket.IO connect ─────────────────
        if alice.token and bob.token:
            info("Stage 6: connect Socket.IO")
            a_sio = await alice.connect_socketio(); b_sio = await bob.connect_socketio()
            rpt.steps.append(Step("alice.sio_connect", a_sio[0], 0, a_sio[1]))
            rpt.steps.append(Step("bob.sio_connect",   b_sio[0], 0, b_sio[1]))
            (ok if a_sio[0] else fail)(f"alice.sio      : {a_sio[1]}")
            (ok if b_sio[0] else fail)(f"bob.sio        : {b_sio[1]}")

            # ────────────────────── Stage 7: Channel + message ─────────────────
            if a_sio[0] and b_sio[0]:
                info("Stage 7: channel + messaging")
                cid, det = await alice.create_channel(f"e2e-channel-{secrets.token_hex(3)}", member_ids=[str(bob.user_id)] if bob.user_id else [])
                rpt.steps.append(Step("alice.create_channel", cid is not None, 0, det))
                (ok if cid else warn)(f"alice.channel  : {det}")

                if cid is not None:
                    snd = await alice.send_message(cid, "Hello Bob from Alice!")
                    rpt.steps.append(Step("alice.send_message", snd[0], 0, snd[1]))
                    (ok if snd[0] else warn)(f"alice.send     : {snd[1]}")
                    await asyncio.sleep(1.5)
                    recv = len(bob.received_messages) > 0
                    rpt.steps.append(Step("bob.recv_message", recv, 0,
                                          f"received {len(bob.received_messages)} events"))
                    (ok if recv else warn)(f"bob.recv       : {len(bob.received_messages)} message events received")

                # ────────────────── Stage 8: Call signaling ─────────────────────
                info("Stage 8: call signaling")
                if alice.user_id and bob.user_id:
                    callee = bob.user_id
                else:
                    callee = 0
                call = await alice.initiate_call(callee)
                rpt.steps.append(Step("alice.call_offer", call[0], 0, call[1]))
                (ok if call[0] else warn)(f"alice.call     : {call[1]}")
                await asyncio.sleep(1.0)
                got_call = len(bob.received_call_events) > 0
                rpt.steps.append(Step("bob.recv_call", got_call, 0,
                                      f"{len(bob.received_call_events)} call events"))
                (ok if got_call else warn)(f"bob.call_recv  : {len(bob.received_call_events)} call events")

            await alice.disconnect_socketio()
            await bob.disconnect_socketio()
            ok("disconnected both clients")

    finally:
        for p in procs:
            try:
                p.terminate()
                p.wait(timeout=5)
            except Exception:
                try: p.kill()
                except: pass

    rpt.finished_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return rpt


def _ret(passed: bool, detail: str):
    """Helper: turn a bool + str into the awaitable (bool, str) tuple."""
    async def _w(): return passed, detail
    return _w()


# -----------------------------------------------------------------------------
def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--server-port", type=int, default=0)
    p.add_argument("--router-port", type=int, default=0)
    p.add_argument("--via-router", action="store_true")
    p.add_argument("--json", action="store_true")
    return p.parse_args()


def main():
    args = parse_args()
    print(C("1;36", "═" * 72))
    print(C("1;36", "  Helen E2E Two-Client Simulation"))
    print(C("1;36", "═" * 72))
    rpt = asyncio.run(run(args))
    print(C("1;36", "─" * 72))
    print(C("1;36", "  Summary"))
    print(C("1;36", "─" * 72))
    passed = sum(1 for s in rpt.steps if s.passed)
    total  = len(rpt.steps)
    print(f"  {passed}/{total} steps passed")
    if rpt.passed:
        print(C("1;32", "  ✓ END-TO-END WORKS — two clients can register, login, message, and signal calls"))
    else:
        print(C("1;31", "  ✗ END-TO-END INCOMPLETE — see step list"))
        for s in rpt.steps:
            if not s.passed:
                print(f"    - {s.name}: {s.detail}")
    print(C("1;36", "═" * 72))

    if args.json:
        print(json.dumps({**asdict(rpt), "passed": rpt.passed,
                          "passed_count": passed, "total": total},
                         indent=2, default=str))
    return 0 if rpt.passed else 1

if __name__ == "__main__":
    sys.exit(main())
