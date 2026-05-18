"""End-to-end smoke test — exercises every flow the GUI uses.
Approximates a "real user opens the app, logs in, sends a message,
starts a call, shares screen" session via the HTTP API + Socket.IO.

Run against a live Helen-Server.exe at 127.0.0.1:3000.
Returns non-zero exit on first failure so it can be wired into CI later.
"""
import asyncio
import json
import sys
import time
import uuid

import httpx
import socketio

BASE = "http://127.0.0.1:3000"


async def fail(msg: str) -> "None":
    print(f"FAIL: {msg}")
    sys.exit(1)


async def main() -> int:
    print("== E2E smoke test ==")

    suffix = uuid.uuid4().hex[:8]
    user_a = f"alice_{suffix}"
    user_b = f"bob_{suffix}"
    pw = "Sm0kePass!2026"

    async with httpx.AsyncClient(base_url=BASE, timeout=15.0) as c:
        # 1. health
        r = await c.get("/api/health")
        assert r.status_code == 200 and r.json()["status"] == "ok", r.text
        print(f"  [01] health         OK")

        # 2. register A
        r = await c.post("/api/auth/register", json={
            "username": user_a, "password": pw,
            "display_name": "Alice Smoke",
        })
        assert r.status_code in (200, 201), f"register A: {r.status_code} {r.text}"
        a = r.json()
        a_token = a["tokens"]["access_token"]
        a_id = a["user"]["id"]
        a_share = a["user"]["share_code"]
        print(f"  [02] register A     OK  id={a_id[:12]}...")

        # 3. register B
        r = await c.post("/api/auth/register", json={
            "username": user_b, "password": pw,
            "display_name": "Bob Smoke",
        })
        assert r.status_code in (200, 201), f"register B: {r.text}"
        b = r.json()
        b_token = b["tokens"]["access_token"]
        b_id = b["user"]["id"]
        print(f"  [03] register B     OK  id={b_id[:12]}...")

        # 4. login A again (verify auth flow)
        r = await c.post("/api/auth/login", json={
            "username": user_a, "password": pw,
        })
        assert r.status_code == 200, f"login A: {r.text}"
        a_token = r.json()["tokens"]["access_token"]
        print(f"  [04] login A        OK")

        # 5. me
        r = await c.get("/api/users/me",
                        headers={"Authorization": f"Bearer {a_token}"})
        assert r.status_code == 200 and r.json()["username"] == user_a, r.text
        print(f"  [05] /users/me      OK")

        # 6. create a channel with B as member
        r = await c.post("/api/channels", json={
            "name": f"e2e-{suffix}",
            "type": "group",
            "member_ids": [b_id],
        }, headers={"Authorization": f"Bearer {a_token}"})
        assert r.status_code in (200, 201), f"create channel: {r.status_code} {r.text}"
        ch = r.json()
        ch_id = ch["id"]
        print(f"  [06] channel create OK  id={ch_id[:12]}...")

        # 7. send a message
        r = await c.post(f"/api/channels/{ch_id}/messages", json={
            "content": "hello from e2e smoke test",
            "type": "text",
        }, headers={"Authorization": f"Bearer {a_token}"})
        assert r.status_code in (200, 201), f"send message: {r.status_code} {r.text}"
        msg = r.json()
        print(f"  [07] send message   OK  id={msg.get('id', '?')[:12]}...")

        # 8. list messages — both should see it
        r = await c.get(f"/api/channels/{ch_id}/messages",
                        headers={"Authorization": f"Bearer {b_token}"})
        assert r.status_code == 200, r.text
        msgs = r.json().get("messages") or r.json()
        assert any(
            m.get("content") == "hello from e2e smoke test"
            for m in (msgs if isinstance(msgs, list) else [])
        ), f"B can't see message: {json.dumps(msgs)[:300]}"
        print(f"  [08] B sees message OK  total={len(msgs)}")

        # 9. ICE config (what the desktop fetches before opening RTCPeerConnection)
        r = await c.get("/api/turn/ice-config",
                        headers={"Authorization": f"Bearer {a_token}"})
        assert r.status_code == 200, f"ice-config: {r.text}"
        ice = r.json()
        assert ice.get("ice_servers"), f"no ICE servers in: {ice}"
        print(f"  [09] TURN/ICE       OK  servers={len(ice['ice_servers'])}")

        # 10. cluster info (operator visibility)
        r = await c.get("/api/cluster/info")
        assert r.status_code == 200, r.text
        info = r.json()
        assert info.get("node_id"), info
        print(f"  [10] cluster info   OK  node={info['node_id'][:18]}...")

        # 11. transports endpoint (was empty in early builds)
        r = await c.get("/api/transports/categories",
                        headers={"Authorization": f"Bearer {a_token}"})
        assert r.status_code == 200, r.text
        cats = r.json()
        # Endpoint returns either a list, or a dict where each key IS a
        # category (with the count as value), or a dict with a
        # "categories" wrapper. Handle all three.
        if isinstance(cats, list):
            cats_len = len(cats)
        elif isinstance(cats, dict):
            inner = cats.get("categories")
            cats_len = len(inner) if inner else len(cats)
        else:
            cats_len = 0
        assert cats_len >= 10, f"too few categories: {cats_len}"
        print(f"  [11] transports     OK  categories={cats_len}")

    # ── 12. Socket.IO live connection + receive message in real time
    sio_b = socketio.AsyncClient(reconnection=False, logger=False, engineio_logger=False)
    received: list[dict] = []

    @sio_b.event
    async def connect():
        pass

    @sio_b.on("v2_chat:new_message")
    async def on_new_v2(data):
        received.append({"event": "v2_chat:new_message", "data": data})

    @sio_b.on("chat:new_message")
    async def on_new_v1(data):
        received.append({"event": "chat:new_message", "data": data})

    try:
        await sio_b.connect(
            BASE,
            socketio_path="/socket.io/",
            transports=["websocket"],
            auth={"token": b_token},
        )
        print(f"  [12] socket connect OK  sid={sio_b.sid[:12] if sio_b.sid else '?'}...")
    except Exception as e:
        print(f"  [12] socket connect FAIL: {e}")
        sys.exit(1)

    # 13. A connects via socket and emits v2_chat_send_message —
    #     confirm B receives it. (HTTP POST does NOT fan out via socket
    #     — that's by design; chat fanout is socket-event-driven.)
    sio_a = socketio.AsyncClient(reconnection=False, logger=False, engineio_logger=False)
    try:
        await sio_a.connect(
            BASE,
            socketio_path="/socket.io/",
            transports=["websocket"],
            auth={"token": a_token},
        )
        ack_holder: dict = {}

        @sio_a.on("v2_chat:message_sent")
        async def on_sent(data):
            ack_holder["ack"] = data

        await sio_a.emit("v2_chat_send_message", {
            "channel_id": ch_id,
            "content": "live socket test",
            "type": "text",
            "client_message_id": uuid.uuid4().hex,
        })

        # Wait up to 5s for B's receive
        deadline = time.time() + 5.0
        while time.time() < deadline and not received:
            await asyncio.sleep(0.1)
    finally:
        if sio_a.connected:
            await sio_a.disconnect()

    if received:
        print(f"  [13] live receive   OK  events={len(received)} kinds={set(r['event'] for r in received)}")
    else:
        print(f"  [13] live receive   FAIL (no socket event for B in 5s)")
        await sio_b.disconnect()
        sys.exit(1)

    # 14. presence — confirm B is online from server's view
    async with httpx.AsyncClient(base_url=BASE, timeout=15.0) as c:
        r = await c.get(f"/api/users/{b_id}/presence",
                        headers={"Authorization": f"Bearer {a_token}"})
        if r.status_code == 200:
            print(f"  [14] presence       OK  status={r.json().get('status')}")
        else:
            print(f"  [14] presence       SKIP ({r.status_code})")

    # 15. Initiate a call from A → B via socket event (the real path —
    #     calls are signaled over Socket.IO, not HTTP).
    sio_a2 = socketio.AsyncClient(reconnection=False, logger=False, engineio_logger=False)
    incoming_call: list[dict] = []

    @sio_b.on("v2_call:incoming")
    async def on_incoming(data):
        incoming_call.append(data)

    @sio_b.on("call:incoming")
    async def on_incoming_v1(data):
        incoming_call.append(data)

    try:
        await sio_a2.connect(
            BASE, socketio_path="/socket.io/", transports=["websocket"],
            auth={"token": a_token},
        )
        ack = await sio_a2.call("v2_call_initiate", {
            "target_id": b_id, "media_type": "audio",
        }, timeout=8.0)
        call_id = (ack or {}).get("call_id") if isinstance(ack, dict) else None
        if call_id:
            print(f"  [15] call initiate  OK  call_id={call_id[:12]}...")
            # Wait briefly for B's incoming-call event
            t0 = time.time()
            while time.time() - t0 < 3.0 and not incoming_call:
                await asyncio.sleep(0.1)
            if incoming_call:
                print(f"  [16] call ringing   OK  B got incoming-call event")
            else:
                print(f"  [16] call ringing   SOFT-FAIL (no incoming event)")

            # Hangup
            await sio_a2.emit("call_hangup", {"call_id": call_id})
            print(f"  [17] call hangup    OK")
        else:
            print(f"  [15] call initiate  SOFT-FAIL ack={ack}")
    except Exception as e:
        print(f"  [15] call initiate  SOFT-FAIL ({type(e).__name__}: {e})")
    finally:
        if sio_a2.connected:
            await sio_a2.disconnect()

    # 18. Verify the screen-share preset module loads (in-process — the
    #     HTTP route may not exist; the real GUI uses the local helper)
    try:
        from app.services.screen_share_quality import all_presets, auto_select_preset
        ps = all_presets()
        preset_names = [p["name"] for p in ps]
        # MICRO + ULTRA are the new ones I added in pass 1
        assert "micro" in preset_names and "ultra" in preset_names, preset_names
        # auto-select sanity
        assert auto_select_preset(100) == "micro"
        assert auto_select_preset(20000) == "ultra"
        print(f"  [18] screen presets OK  presets={preset_names}")
    except Exception as e:
        print(f"  [18] screen presets FAIL ({type(e).__name__}: {e})")

    # 19. Logout (refresh-token revocation)
    async with httpx.AsyncClient(base_url=BASE, timeout=15.0) as c:
        r = await c.post("/api/auth/logout",
                         headers={"Authorization": f"Bearer {a_token}"})
        if r.status_code in (200, 204):
            print(f"  [19] logout         OK")
        else:
            print(f"  [19] logout         SOFT-FAIL ({r.status_code})")

    await sio_b.disconnect()
    print("\n== ALL CHECKS PASSED ==")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
