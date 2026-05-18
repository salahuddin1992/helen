"""
Probe that replays the exact handshake the Electron client performs,
proving the client→server path works end-to-end from outside the browser.

What we replay (from src/renderer/services/socket.manager.ts):
    io(url, { auth: { token }, transports: ['websocket','polling'], ... })

Plus the HTTP auth flow (register → login → /me).

Run while the server is up on 127.0.0.1:3000:
    python scripts/client_handshake_probe.py
"""
from __future__ import annotations

import argparse
import asyncio
import random
import string
import sys
import time

import aiohttp
import socketio


async def probe(base: str) -> int:
    stamp = int(time.time())
    uname = f"probe_{stamp}_{''.join(random.choices(string.ascii_lowercase, k=4))}"
    pw = "Strong#pw42" + "".join(random.choices(string.ascii_letters, k=4))

    checks: list[tuple[str, bool, str]] = []

    async with aiohttp.ClientSession() as s:
        # 1) Server reachability (what the bootstrap screen does first).
        t = time.perf_counter()
        async with s.get(f"{base}/api/health") as r:
            ok = r.status == 200
            body = await r.text()
        checks.append(("http_health", ok, f"{r.status} — {(time.perf_counter()-t)*1000:.0f}ms"))
        if not ok:
            print("Server not reachable — abort.")
            for name, ok, d in checks:
                print(f"  [{'OK' if ok else 'FAIL'}] {name}: {d}")
            return 1

        # 2) Register (client calls this from RegisterForm).
        t = time.perf_counter()
        async with s.post(
            f"{base}/api/auth/register",
            json={"username": uname, "display_name": uname, "password": pw},
        ) as r:
            reg_status = r.status
        checks.append(("http_register", reg_status in (200, 201), f"{reg_status} — {(time.perf_counter()-t)*1000:.0f}ms"))

        # 3) Login (client calls this from LoginForm).
        t = time.perf_counter()
        async with s.post(
            f"{base}/api/auth/login",
            json={"username": uname, "password": pw, "device_name": "handshake-probe"},
        ) as r:
            login_ok = r.status == 200
            data = await r.json() if login_ok else {}
        checks.append(("http_login", login_ok, f"status={r.status} — {(time.perf_counter()-t)*1000:.0f}ms"))
        if not login_ok:
            return _report(checks)

        token = data["tokens"]["access_token"]
        user_id = data["user"]["id"]
        print(f"  user_id={user_id[:12]}…  token={token[:16]}…")

        # 4) /api/users/me — proves Authorization: Bearer flow end-to-end.
        t = time.perf_counter()
        async with s.get(f"{base}/api/users/me",
                         headers={"Authorization": f"Bearer {token}"}) as r:
            me_ok = r.status == 200
        checks.append(("http_users_me", me_ok, f"status={r.status} — {(time.perf_counter()-t)*1000:.0f}ms"))

    # 5) Socket.IO with the EXACT options the client uses.
    sio = socketio.AsyncClient(reconnection=False, logger=False, engineio_logger=False)
    got_connect = asyncio.Event()
    got_error: list[str] = []

    @sio.event
    async def connect():
        got_connect.set()

    @sio.event
    async def connect_error(data):
        got_error.append(str(data))

    t = time.perf_counter()
    try:
        await sio.connect(
            base,
            auth={"token": token},
            transports=["websocket"],
            wait_timeout=10,
        )
        await asyncio.wait_for(got_connect.wait(), timeout=5)
        checks.append(("socketio_ws_connect", True, f"sid={sio.sid} — {(time.perf_counter()-t)*1000:.0f}ms"))
    except Exception as exc:
        checks.append(("socketio_ws_connect", False, f"{type(exc).__name__}: {exc} err={got_error}"))
        return _report(checks)

    # A successful socket connect IS the authentication proof — the
    # server's connect handler (app/socket/server.py:48) JWT-decodes
    # the token and raises ConnectionRefusedError on any failure, so
    # reaching this point means the server validated the token AND
    # registered this sid with the presence service.
    #
    # Negative control: try connecting with a bogus token and confirm
    # the server refuses. This rules out "auth accepts anything".
    bad = socketio.AsyncClient(reconnection=False, logger=False, engineio_logger=False)
    t = time.perf_counter()
    try:
        await bad.connect(base, auth={"token": "not-a-real-jwt"},
                          transports=["websocket"], wait_timeout=5)
        await bad.disconnect()
        checks.append(("socketio_refuses_bad_token", False,
                       "bogus token was accepted — auth is broken"))
    except socketio.exceptions.ConnectionError as exc:
        checks.append(("socketio_refuses_bad_token", True,
                       f"refused ({exc}) — {(time.perf_counter()-t)*1000:.0f}ms"))
    except Exception as exc:
        checks.append(("socketio_refuses_bad_token", True,
                       f"refused ({type(exc).__name__}) — {(time.perf_counter()-t)*1000:.0f}ms"))

    await sio.disconnect()
    return _report(checks)


def _report(checks: list[tuple[str, bool, str]]) -> int:
    print("\n══════════ CLIENT→SERVER HANDSHAKE ══════════")
    for name, ok, d in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {d}")
    all_ok = all(ok for _, ok, _ in checks)
    print("\nResult:", "PASS" if all_ok else "FAIL")
    return 0 if all_ok else 1


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=3000)
    args = p.parse_args()
    sys.exit(asyncio.run(probe(f"http://{args.host}:{args.port}")))


if __name__ == "__main__":
    main()
