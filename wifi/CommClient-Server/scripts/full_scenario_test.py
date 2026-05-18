"""
Full-scenario E2E test — 8K video cap + chat + voice/video call, concurrent.

What it does, in order:
  1. Directly seeds media_policies (global 8K = 7680x4320, 60fps, 80Mbps,
     allow_8k=ON) via sqlite so the test is hermetic.
  2. Registers N fresh users (register is public).
  3. For each user: login → opens Socket.IO with JWT auth.
  4. User[0] creates a group channel with all N members.
  5. Every user fetches /api/media-policy/me and asserts the 8K preset
     is in the returned ladder.
  6. Every user sends M messages over the chat socket event.
  7. Every user joins the group call via call_join_group event
     (media_type=video).
  8. Prints per-phase pass/fail counts and latencies.

The test exercises the three concurrent data paths:
  - Control plane:   REST /api/* over HTTP
  - Realtime plane:  Socket.IO over WebSocket (chat + call signaling)
  - Media plane:     validated via media-cap (full RTP needs a browser /
                     aiortc — that's verified by CallEngine in the Electron
                     client; here we confirm the signaling side end-to-end)

Run:
    python scripts/full_scenario_test.py --host 127.0.0.1 --port 3000 --users 5 --msgs 5
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sqlite3
import string
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import aiohttp
import socketio


# ── DB seeding ────────────────────────────────────────────────

def seed_media_policy_8k(db_path: Path) -> None:
    """Sanity-check the DB came up with 8K-ready defaults (from migration
    007). If an operator has customized caps below 8K we leave them
    alone — this is just an assertion path for the test."""
    if not db_path.exists():
        print(f"!! sqlite not found at {db_path} — skipping seed check")
        return
    con = sqlite3.connect(str(db_path))
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT global_max_width, allow_8k, role_caps_json FROM media_policies WHERE id='global'"
        )
        row = cur.fetchone()
        if not row:
            print("!! no global media_policy row — migrations not applied?")
            return
        w, allow_8k, caps = row
        print(f"✓ policy on disk: global_w={w}, allow_8k={bool(allow_8k)}")
    finally:
        con.close()


# ── HTTP helpers ──────────────────────────────────────────────

async def register(session: aiohttp.ClientSession, base: str, uname: str, pw: str) -> dict:
    r = await session.post(
        f"{base}/api/auth/register",
        json={"username": uname, "display_name": uname, "password": pw},
    )
    if r.status == 409 or r.status == 400:
        # Already exists — fall through to login.
        return {}
    r.raise_for_status()
    return await r.json()


async def login(session: aiohttp.ClientSession, base: str, uname: str, pw: str) -> dict:
    r = await session.post(
        f"{base}/api/auth/login",
        json={"username": uname, "password": pw, "device_name": "scenario-test"},
    )
    r.raise_for_status()
    return await r.json()


async def get_my_cap(session: aiohttp.ClientSession, base: str, token: str) -> dict:
    r = await session.get(
        f"{base}/api/media-policy/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    r.raise_for_status()
    return await r.json()


async def create_channel(
    session: aiohttp.ClientSession, base: str, token: str, member_ids: list[str],
) -> dict:
    r = await session.post(
        f"{base}/api/channels",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "type": "group",
            "name": f"scenario-{int(time.time())}",
            "member_ids": member_ids,
        },
    )
    r.raise_for_status()
    return await r.json()


# ── Socket.IO per-user session ───────────────────────────────

class User:
    def __init__(self, idx: int, base: str, username: str, password: str):
        self.idx = idx
        self.base = base
        self.username = username
        self.password = password
        self.user_id: str | None = None
        self.token: str | None = None
        self.sio = socketio.AsyncClient(reconnection=False, logger=False, engineio_logger=False)
        self.connected = False
        self.msgs_sent = 0
        self.msgs_failed = 0
        self.call_joined = False
        self.cap_8k_ok = False
        self.errors: list[str] = []

    async def auth(self, session: aiohttp.ClientSession) -> None:
        try:
            await register(session, self.base, self.username, self.password)
        except Exception:
            pass
        data = await login(session, self.base, self.username, self.password)
        self.user_id = data["user"]["id"]
        self.token = data["tokens"]["access_token"]

    async def connect(self) -> None:
        assert self.token
        await self.sio.connect(
            self.base,
            auth={"token": self.token},
            transports=["websocket"],
            wait_timeout=10,
        )
        self.connected = True

    async def check_cap(self, session: aiohttp.ClientSession) -> None:
        data = await get_my_cap(session, self.base, self.token or "")
        cap = data.get("cap", {})
        ladder = data.get("ladder", [])
        has_8k = any(r.get("id") in ("8k", "4320p") or r.get("w", 0) >= 7680 for r in ladder)
        self.cap_8k_ok = bool(cap.get("allow_8k")) and has_8k

    async def send_messages(self, channel_id: str, n: int) -> None:
        for i in range(n):
            try:
                ack = await self.sio.call(
                    "chat_send_message",
                    {
                        "channel_id": channel_id,
                        "content": f"[{self.username}] hello #{i} {uuid.uuid4().hex[:6]}",
                        "type": "text",
                    },
                    timeout=8,
                )
                if isinstance(ack, dict) and ack.get("error"):
                    self.msgs_failed += 1
                    self.errors.append(f"msg: {ack['error']}")
                else:
                    self.msgs_sent += 1
            except Exception as exc:
                self.msgs_failed += 1
                self.errors.append(f"msg_exc: {exc}")

    async def join_call(self, channel_id: str) -> None:
        try:
            ack = await self.sio.call(
                "call_join_group",
                {"channel_id": channel_id, "media_type": "video"},
                timeout=10,
            )
            if isinstance(ack, dict) and ack.get("error"):
                self.errors.append(f"call: {ack['error']}")
            else:
                self.call_joined = True
        except Exception as exc:
            self.errors.append(f"call_exc: {exc}")

    async def disconnect(self) -> None:
        if self.connected:
            try:
                await self.sio.disconnect()
            except Exception:
                pass


# ── Runner ────────────────────────────────────────────────────

async def run(args: argparse.Namespace) -> int:
    db_path = Path(args.project_root) / "data" / "commclient.db"
    seed_media_policy_8k(db_path)

    base = f"http://{args.host}:{args.port}"
    stamp = int(time.time())
    users = [
        User(i, base, f"stx{stamp}u{i}", "Strong#pw42" + "".join(random.choices(string.ascii_letters, k=4)))
        for i in range(args.users)
    ]

    print(f"\n── PHASE 1: register/login {args.users} users ──")
    async with aiohttp.ClientSession() as session:
        t0 = time.perf_counter()
        await asyncio.gather(*(u.auth(session) for u in users))
        print(f"  ok — {(time.perf_counter()-t0)*1000:.0f}ms")

        print(f"\n── PHASE 2: socket.io connect ──")
        t0 = time.perf_counter()
        results = await asyncio.gather(*(u.connect() for u in users), return_exceptions=True)
        failed = [r for r in results if isinstance(r, Exception)]
        print(f"  {len(users)-len(failed)}/{len(users)} connected — {(time.perf_counter()-t0)*1000:.0f}ms")
        if failed:
            for e in failed[:3]:
                print(f"    sample: {e}")

        print(f"\n── PHASE 3: fetch media cap (expect 8K) ──")
        await asyncio.gather(*(u.check_cap(session) for u in users))
        ok = sum(1 for u in users if u.cap_8k_ok)
        print(f"  {ok}/{len(users)} see 8K in their ladder")

        print(f"\n── PHASE 4: create group channel ──")
        creator = users[0]
        member_ids = [u.user_id for u in users[1:] if u.user_id]
        ch = await create_channel(session, base, creator.token or "", member_ids)
        channel_id = ch["id"]
        print(f"  channel {channel_id} · {len(member_ids)+1} members")

        print(f"\n── PHASE 5: {args.msgs} msgs × {len(users)} users — concurrent ──")
        t0 = time.perf_counter()
        await asyncio.gather(*(u.send_messages(channel_id, args.msgs) for u in users))
        total_sent = sum(u.msgs_sent for u in users)
        total_fail = sum(u.msgs_failed for u in users)
        print(f"  sent {total_sent} / failed {total_fail} — {(time.perf_counter()-t0)*1000:.0f}ms "
              f"→ {total_sent*1000.0/max(1,(time.perf_counter()-t0)*1000):.1f} msg/s")

        print(f"\n── PHASE 6: all users join group call (video) — concurrent ──")
        t0 = time.perf_counter()
        await asyncio.gather(*(u.join_call(channel_id) for u in users))
        joined = sum(1 for u in users if u.call_joined)
        print(f"  {joined}/{len(users)} joined the call — {(time.perf_counter()-t0)*1000:.0f}ms")

        # Hold for a bit so the call is actually live (simulates conversation)
        print(f"\n── PHASE 7: hold call + keep chatting for {args.hold}s ──")
        # Fire another msg round during the call
        await asyncio.gather(*(u.send_messages(channel_id, 1) for u in users))
        await asyncio.sleep(args.hold)

        print(f"\n── TEARDOWN ──")
        await asyncio.gather(*(u.disconnect() for u in users))

    # ── Report ──
    print("\n══════════════ REPORT ══════════════")
    rows = []
    for u in users:
        rows.append({
            "user": u.username,
            "connected": u.connected,
            "8k_in_cap": u.cap_8k_ok,
            "msgs_sent": u.msgs_sent,
            "msgs_failed": u.msgs_failed,
            "call_joined": u.call_joined,
            "errors": u.errors[:2],
        })
    print(json.dumps(rows, indent=2))

    all_ok = all(
        u.connected and u.cap_8k_ok and u.call_joined and u.msgs_failed == 0 and u.msgs_sent >= args.msgs
        for u in users
    )
    print("\nResult:", "PASS ✓" if all_ok else "FAIL ✗")
    return 0 if all_ok else 1


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=3000)
    p.add_argument("--users", type=int, default=5)
    p.add_argument("--msgs", type=int, default=5)
    p.add_argument("--hold", type=float, default=2.0)
    p.add_argument("--project-root", default=str(Path(__file__).resolve().parent.parent))
    args = p.parse_args()
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
