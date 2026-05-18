"""
Full multi-user chat simulation against the running CommClient-Server.

Spawns 5 virtual users who:
  - register + log in via REST
  - connect to Socket.IO with JWT auth
  - set presence (online / away / busy)
  - create a group channel together
  - chat back and forth in real time (Socket.IO)
  - send typing indicators
  - add reactions
  - edit a message
  - mark messages as read
  - upload a file via REST
  - create a poll
  - start + accept a 1:1 call
  - exercise the new features (drafts, templates, categories, schedule, permissions)

Prints a colored, timestamped activity log so you can see the server working
end-to-end in real time. Assumes the server is already running on port 3007.
"""

from __future__ import annotations

import asyncio
import json
import random
import string
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import socketio

BASE_URL = "http://127.0.0.1:3007"
API = BASE_URL + "/api"

# Windows cmd.exe doesn't support ANSI by default; fall back gracefully.
try:
    import colorama  # type: ignore
    colorama.just_fix_windows_console()
    COLOR = True
except Exception:
    COLOR = True  # modern Windows terminals handle ANSI fine


def _c(code: str, text: str) -> str:
    if not COLOR:
        return text
    return f"\033[{code}m{text}\033[0m"


def dim(t: str) -> str:     return _c("90", t)
def green(t: str) -> str:   return _c("32", t)
def yellow(t: str) -> str:  return _c("33", t)
def blue(t: str) -> str:    return _c("34", t)
def magenta(t: str) -> str: return _c("35", t)
def cyan(t: str) -> str:    return _c("36", t)
def red(t: str) -> str:     return _c("31", t)
def bold(t: str) -> str:    return _c("1",  t)


START_TS = time.time()


def log(tag: str, msg: str, color=cyan) -> None:
    t = f"{time.time() - START_TS:6.2f}s"
    print(f"{dim(t)} {color(tag.ljust(12))} {msg}", flush=True)


USER_COLORS = [green, yellow, blue, magenta, cyan]


# ─────────────────────────────────────────────────────────────
# User abstraction
# ─────────────────────────────────────────────────────────────


@dataclass
class SimUser:
    username: str
    display_name: str
    color_idx: int
    user_id: str = ""
    token: str = ""
    sio: socketio.AsyncClient = field(default_factory=socketio.AsyncClient)
    inbox: list[dict] = field(default_factory=list)
    typing_from: set[str] = field(default_factory=set)

    def paint(self, text: str) -> str:
        return USER_COLORS[self.color_idx % len(USER_COLORS)](text)

    @property
    def label(self) -> str:
        return self.paint(self.display_name)


async def register(client: httpx.AsyncClient, u: SimUser) -> None:
    r = await client.post(
        "/auth/register",
        json={
            "username": u.username,
            "password": "Simulate!" + "".join(random.choices(string.ascii_letters, k=6)),
            "display_name": u.display_name,
        },
    )
    r.raise_for_status()
    body = r.json()
    u.token = body.get("access_token") or body.get("tokens", {}).get("access_token", "")
    u.user_id = body.get("user", {}).get("id") or body.get("id")
    assert u.token and u.user_id, f"bad register: {body}"


async def connect_socket(u: SimUser) -> None:
    """Open a Socket.IO connection authenticated as this user."""

    @u.sio.event
    async def connect() -> None:
        log("socket", f"{u.label} connected", dim)

    @u.sio.event
    async def disconnect() -> None:
        log("socket", f"{u.label} disconnected", dim)

    @u.sio.on("chat:new_message")
    async def on_new_message(data: dict) -> None:
        sender = (data.get("sender") or {}).get("display_name", "?")
        content = data.get("content", "")
        u.inbox.append(data)
        log("recv", f"{u.label} <- {sender}: {content}", dim)

    @u.sio.on("chat:typing_start")
    async def on_typing_start(data: dict) -> None:
        uid = data.get("user_id") or data.get("sender_id")
        if uid:
            u.typing_from.add(uid)

    @u.sio.on("chat:typing_stop")
    async def on_typing_stop(data: dict) -> None:
        uid = data.get("user_id") or data.get("sender_id")
        u.typing_from.discard(uid or "")

    @u.sio.on("chat:message_edited")
    async def on_edit(data: dict) -> None:
        log("recv", f"{u.label} saw an edit: '{data.get('content','')[:40]}'", dim)

    @u.sio.on("chat:reaction_added")
    async def on_react(data: dict) -> None:
        log("recv", f"{u.label} saw reaction {data.get('emoji','?')}", dim)

    @u.sio.on("call:incoming")
    async def on_incoming(data: dict) -> None:
        log("call", f"{u.label} <- incoming call from {data.get('caller_id','?')[:8]}", magenta)

    @u.sio.on("call:accepted")
    async def on_accepted(data: dict) -> None:
        log("call", f"{u.label} sees call accepted", magenta)

    @u.sio.on("presence:update")
    async def on_presence(data: dict) -> None:
        pass

    await u.sio.connect(
        BASE_URL,
        socketio_path="/socket.io",
        auth={"token": u.token},
        transports=["websocket", "polling"],
        wait_timeout=10,
    )


def auth_headers(u: SimUser) -> dict[str, str]:
    return {"Authorization": f"Bearer {u.token}"}


# ─────────────────────────────────────────────────────────────
# Scenario
# ─────────────────────────────────────────────────────────────


async def scenario() -> int:
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=5))
    names = ["Alice", "Bob", "Charlie", "Diana", "Eve"]
    users: list[SimUser] = [
        SimUser(username=f"sim_{n.lower()}_{suffix}", display_name=n, color_idx=i)
        for i, n in enumerate(names)
    ]

    print(bold("\n══ CommClient-Server multi-user simulation ══\n"))
    log("setup", f"Registering {len(users)} users against {BASE_URL}", cyan)

    async with httpx.AsyncClient(base_url=API, timeout=15) as client:
        # Register everyone
        for u in users:
            await register(client, u)
            log("auth", f"{u.label} registered (id={u.user_id[:8]}…)", green)

        # Connect Socket.IO for everyone
        log("setup", "Opening Socket.IO connections…", cyan)
        await asyncio.gather(*(connect_socket(u) for u in users))

        # Presence statuses
        await users[0].sio.emit("presence_set_status", {"status": "online"})
        await users[1].sio.emit("presence_set_status", {"status": "online"})
        await users[2].sio.emit("presence_set_status", {"status": "busy"})
        await users[3].sio.emit("presence_set_status", {"status": "away"})
        await users[4].sio.emit("presence_set_status", {"status": "online"})
        log("presence", "Alice/Bob/Eve=online  Charlie=busy  Diana=away", yellow)

        # Alice creates a group channel with everyone
        alice = users[0]
        member_ids = [u.user_id for u in users[1:]]
        r = await client.post(
            "/channels",
            headers=auth_headers(alice),
            json={"type": "group", "name": f"#general-{suffix}", "member_ids": member_ids},
        )
        r.raise_for_status()
        group = r.json()
        group_id = group.get("id") or group.get("channel", {}).get("id")
        log("channel", f"Alice created group {group.get('name','')} ({group_id[:8]}…)", green)

        # Let all clients see the channel membership propagate
        await asyncio.sleep(0.3)

        # ── Live chat round ──
        script = [
            (0, "Hey team 👋  Everyone here?"),
            (1, "Yep — morning!"),
            (2, "Present (buried in tickets)"),
            (4, "Here 🙂"),
            (0, "I pushed the permissions-matrix branch last night"),
            (1, "Saw it. Tests green?"),
            (0, "80/80"),
            (2, "Nice, I'll pull it into staging after standup"),
            (4, "Do we need a migration?"),
            (0, "Two new tables — channel_role_permission + channel_member_permission"),
            (1, "I'll write the release note"),
            (2, "Also — can someone take over #incident-42? I'm full today"),
            (4, "I'll grab it"),
            (0, "Thanks Eve ❤️"),
        ]

        log("chat", "── live chat begins ──", bold)
        last_msg_id: str | None = None
        first_msg_id: str | None = None

        for i, (uidx, text) in enumerate(script):
            sender = users[uidx]
            # typing indicator
            await sender.sio.emit("chat_typing_start", {"channel_id": group_id})
            await asyncio.sleep(random.uniform(0.12, 0.25))
            await sender.sio.emit("chat_typing_stop", {"channel_id": group_id})

            # send message via socket (goes through full v1 path)
            ack = await sender.sio.call(
                "chat_send_message",
                {"channel_id": group_id, "content": text, "type": "text"},
                timeout=5,
            )
            mid = None
            if isinstance(ack, dict):
                mid = (ack.get("message") or {}).get("id") or ack.get("id")
            if mid is None:
                # fall back: query latest message via REST
                lr = await client.get(
                    f"/channels/{group_id}/messages?limit=1",
                    headers=auth_headers(sender),
                )
                if lr.status_code == 200:
                    items = lr.json().get("items") or lr.json().get("messages") or lr.json()
                    if isinstance(items, list) and items:
                        mid = items[0].get("id")
            last_msg_id = mid or last_msg_id
            if first_msg_id is None:
                first_msg_id = mid
            log("send", f"{sender.label}: {text}", sender.paint)
            await asyncio.sleep(random.uniform(0.08, 0.18))

        await asyncio.sleep(0.4)

        # ── Reactions ──
        if last_msg_id:
            for u in (users[0], users[1], users[3]):
                await u.sio.emit("chat_reaction", {"message_id": last_msg_id, "emoji": "👍"})
            log("react", f"Alice/Bob/Diana reacted 👍 on last message", yellow)

        # ── Edit a message ──
        if first_msg_id:
            r = await client.patch(
                f"/messages/{first_msg_id}",
                headers=auth_headers(alice),
                json={"content": "Hey team 👋  Everyone here? (edited)"},
            )
            if r.status_code == 200:
                log("edit", f"Alice edited the opener (v2)", yellow)

        # ── Read receipts ──
        if last_msg_id:
            for u in users[1:]:
                await u.sio.emit(
                    "chat_message_read",
                    {"channel_id": group_id, "message_id": last_msg_id},
                )
            log("read", f"{len(users)-1} members marked as read", dim)

        # ── File upload via REST ──
        fake_bytes = ("-- simulated release notes --\n" * 20).encode()
        files = {"file": ("release_notes.txt", fake_bytes, "text/plain")}
        r = await client.post(
            f"/files/upload?channel_id={group_id}",
            headers=auth_headers(alice),
            files=files,
        )
        if r.status_code in (200, 201):
            fj = r.json()
            fid = fj.get("id") or (fj.get("file") or {}).get("id")
            log("file", f"Alice uploaded release_notes.txt id={str(fid)[:8]}…", green)
        else:
            log("file", f"upload returned {r.status_code}: {r.text[:120]}", red)

        # ── Poll ──
        r = await client.post(
            "/polls",
            headers=auth_headers(users[1]),
            json={
                "channel_id": group_id,
                "question": "Lunch spot today?",
                "options": ["Shawarma", "Sushi", "Pizza", "Skip"],
                "is_multi_choice": False,
            },
        )
        if r.status_code in (200, 201):
            poll = r.json()
            poll_id = poll.get("id")
            log("poll", f"Bob created poll: Lunch spot today? ({str(poll_id)[:8]}…)", green)

            # everyone votes
            opts = poll.get("options") or []
            if opts and poll_id:
                for i, u in enumerate(users):
                    opt_id = opts[i % len(opts)].get("id")
                    if opt_id:
                        await client.post(
                            f"/polls/{poll_id}/vote",
                            headers=auth_headers(u),
                            json={"option_ids": [opt_id]},
                        )
                log("poll", f"{len(users)} members voted", green)
        else:
            log("poll", f"poll create {r.status_code}: {r.text[:120]}", red)

        # ── 1:1 call: Alice → Bob ──
        bob = users[1]
        ack = await alice.sio.call(
            "call_initiate",
            {
                "callee_id": bob.user_id,
                "call_type": "audio",
                "sdp_offer": "v=0\no=- 0 0 IN IP4 127.0.0.1\ns=-\nt=0 0\n",
            },
            timeout=5,
        )
        call_id = None
        if isinstance(ack, dict):
            call_id = ack.get("call_id") or (ack.get("call") or {}).get("id")
        log("call", f"Alice → Bob call_initiate ack={str(ack)[:80]}", magenta)

        if call_id:
            await asyncio.sleep(0.3)
            await bob.sio.emit("call_accept", {"call_id": call_id, "sdp_answer": "v=0\n..."})
            log("call", f"Bob accepted call ({call_id[:8]}…)", magenta)
            await asyncio.sleep(0.4)
            await alice.sio.emit("call_hangup", {"call_id": call_id})
            log("call", f"Alice hung up", magenta)

        # ── New-feature exercise ──
        log("features", "── exercising new REST features ──", bold)

        # Draft
        r = await client.put(
            "/drafts",
            headers=auth_headers(alice),
            json={"channel_id": group_id, "content": "thinking about how to phrase this…"},
        )
        if r.status_code in (200, 201):
            log("draft", f"Alice saved a draft for {group_id[:8]}…", cyan)
        else:
            log("draft", f"draft PUT {r.status_code}: {r.text[:120]}", red)

        # Template
        r = await client.post(
            "/templates",
            headers=auth_headers(users[1]),
            json={
                "shortcut": f"standup_{suffix}",
                "title": "Standup template",
                "content": "Yesterday:\n-\nToday:\n-\nBlockers:\n-",
                "scope": "personal",
            },
        )
        if r.status_code in (200, 201):
            log("template", f"Bob created personal template standup_{suffix}", cyan)

        # Channel category
        r = await client.post(
            "/channel-categories",
            headers=auth_headers(alice),
            json={"name": f"Work {suffix}", "color": "#3b82f6"},
        )
        if r.status_code in (200, 201):
            cat = r.json()
            cid = cat.get("id")
            log("category", f"Alice created category 'Work {suffix}'", cyan)
            if cid:
                r2 = await client.post(
                    f"/channel-categories/{cid}/channels",
                    headers=auth_headers(alice),
                    json={"channel_id": group_id},
                )
                if r2.status_code in (200, 201):
                    log("category", f"Alice filed the channel under it", cyan)

        # Schedule + away message
        r = await client.post(
            "/schedule/rules",
            headers=auth_headers(users[2]),  # Charlie
            json={
                "weekday": 1,
                "start_minute": 9 * 60,
                "end_minute": 17 * 60,
                "status": "available",
                "label": "Office hours",
            },
        )
        if r.status_code in (200, 201):
            log("schedule", f"Charlie set Tue 09:00–17:00 office hours", cyan)

        r = await client.put(
            "/schedule/away",
            headers=auth_headers(users[3]),  # Diana
            json={
                "text": "At the dentist until 3pm",
                "is_active": True,
                "mode": "always_away",
            },
        )
        if r.status_code in (200, 201):
            log("schedule", f"Diana set away: 'At the dentist until 3pm'", cyan)

        # Permissions: Alice grants 'pin' to all members
        r = await client.put(
            f"/channels/{group_id}/permissions/role",
            headers=auth_headers(alice),
            json={"role": "member", "permission": "pin", "granted": True},
        )
        if r.status_code in (200, 201):
            log("perms", "Alice granted 'pin' to role=member in this channel", cyan)

        # Verify Bob can now pin (check effective perms)
        r = await client.get(
            f"/channels/{group_id}/permissions/me",
            headers=auth_headers(bob),
        )
        if r.status_code == 200:
            eff = r.json().get("effective", {})
            status = "YES" if eff.get("pin") else "NO"
            log("perms", f"Bob can pin now? {status}", green if eff.get("pin") else red)

        # ── Teardown ──
        log("setup", "closing sockets…", dim)
        await asyncio.gather(*(u.sio.disconnect() for u in users))

    # ── Summary ──
    print()
    print(bold("══ summary ══"))
    total_inbox = sum(len(u.inbox) for u in users)
    print(f"  users          : {len(users)}")
    print(f"  chat messages  : {len(script)}")
    print(f"  inbox events   : {total_inbox} (server → clients)")
    print(f"  elapsed        : {time.time() - START_TS:.2f}s")
    print()
    return 0


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
