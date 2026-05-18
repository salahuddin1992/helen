"""
Server-side media-cap enforcement test.

Proves the principle: the server has higher authority than the client.
A user can lie to their own device all they like — the effective cap
returned by the API still reflects the admin-set policy and per-user
overrides. The UI can only *narrow* further; it can never widen.

What it verifies
----------------
  1. A per-user override seeded directly in the DB is visible to the
     authenticated user through /api/media-policy/me.
  2. The resolution ladder returned by the server is pre-filtered: no
     entry may exceed the effective cap.
  3. A non-admin user gets 403 when touching /api/admin/media-policy
     (client role gating is server-enforced, not a UI convention).
  4. The ladder for a full-cap user still contains 8K (defence against
     accidental cap collapse in migrations).

Run:
    python scripts/cap_enforcement_test.py --host 127.0.0.1 --port 3000
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

import aiohttp


# ── DB helpers ─────────────────────────────────────────────────

def seed_user_override(db_path: Path, user_id: str, *, w: int, h: int, fps: int, kbps: int) -> None:
    """Directly insert a UserMediaOverride row for `user_id`."""
    con = sqlite3.connect(str(db_path))
    try:
        cur = con.cursor()
        cur.execute("DELETE FROM user_media_overrides WHERE user_id=?", (user_id,))
        cur.execute(
            """
            INSERT INTO user_media_overrides
              (id, user_id, max_width, max_height, max_framerate,
               max_bitrate_kbps, note, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (uuid.uuid4().hex[:32], user_id, w, h, fps, kbps, "cap-enforcement-test"),
        )
        con.commit()
    finally:
        con.close()


def clear_user_override(db_path: Path, user_id: str) -> None:
    con = sqlite3.connect(str(db_path))
    try:
        con.cursor().execute("DELETE FROM user_media_overrides WHERE user_id=?", (user_id,))
        con.commit()
    finally:
        con.close()


# ── HTTP helpers ──────────────────────────────────────────────

async def register_login(session: aiohttp.ClientSession, base: str, uname: str, pw: str) -> dict:
    await session.post(
        f"{base}/api/auth/register",
        json={"username": uname, "display_name": uname, "password": pw},
    )
    r = await session.post(
        f"{base}/api/auth/login",
        json={"username": uname, "password": pw, "device_name": "cap-test"},
    )
    r.raise_for_status()
    return await r.json()


async def get_cap(session: aiohttp.ClientSession, base: str, token: str) -> dict:
    r = await session.get(
        f"{base}/api/media-policy/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    r.raise_for_status()
    return await r.json()


async def try_admin_patch(session: aiohttp.ClientSession, base: str, token: str) -> int:
    r = await session.patch(
        f"{base}/api/admin/media-policy",
        headers={"Authorization": f"Bearer {token}"},
        json={"global_max_width": 7680},
    )
    return r.status


# ── Test cases ────────────────────────────────────────────────

class Check:
    def __init__(self, name: str):
        self.name = name
        self.ok = False
        self.detail = ""

    def passed(self, detail: str = "") -> None:
        self.ok = True
        self.detail = detail

    def failed(self, detail: str) -> None:
        self.ok = False
        self.detail = detail


async def run(args: argparse.Namespace) -> int:
    db_path = Path(args.project_root) / "data" / "commclient.db"
    if not db_path.exists():
        print(f"!! sqlite not found at {db_path}")
        return 2

    base = f"http://{args.host}:{args.port}"
    stamp = int(time.time())
    suffix = "".join(random.choices(string.ascii_lowercase, k=4))

    # Two users: one stays unconstrained (full-cap sanity),
    # one gets a 720p@1.5Mbps override.
    low_uname = f"capLow_{stamp}_{suffix}"
    high_uname = f"capHigh_{stamp}_{suffix}"
    pw = "Strong#pw42" + "".join(random.choices(string.ascii_letters, k=4))

    checks: list[Check] = []

    async with aiohttp.ClientSession() as session:
        print(f"\n── step 1: register+login two users ──")
        low = await register_login(session, base, low_uname, pw)
        high = await register_login(session, base, high_uname, pw)
        low_id = low["user"]["id"]
        high_id = high["user"]["id"]
        low_token = low["tokens"]["access_token"]
        high_token = high["tokens"]["access_token"]
        print(f"  low={low_uname[:20]} id={low_id[:8]}")
        print(f"  high={high_uname[:20]} id={high_id[:8]}")

        print(f"\n── step 2: seed per-user override for low user (720p @ 1.5Mbps) ──")
        seed_user_override(db_path, low_id, w=1280, h=720, fps=30, kbps=1500)
        print("  seeded")

        # ── check 1: low user sees the override ──
        print(f"\n── step 3: low user fetches /api/media-policy/me ──")
        low_cap_resp = await get_cap(session, base, low_token)
        cap = low_cap_resp["cap"]
        ladder = low_cap_resp["ladder"]
        print(f"  cap: {cap['max_width']}x{cap['max_height']}@{cap['max_framerate']}fps "
              f"/ {cap['max_bitrate_kbps']}kbps  src={cap['source']}")
        print(f"  ladder: {[r['id'] for r in ladder]}")

        c1 = Check("override applied")
        if cap["source"] == "user_override" and cap["max_width"] == 1280 \
                and cap["max_height"] == 720 and cap["max_bitrate_kbps"] == 1500:
            c1.passed(f"{cap['max_width']}x{cap['max_height']}@{cap['max_bitrate_kbps']}kbps")
        else:
            c1.failed(f"expected 1280x720/1500kbps from user_override, got {cap}")
        checks.append(c1)

        c2 = Check("ladder pre-filtered to ≤720p")
        bad = [r for r in ladder if r["w"] > 1280 or r["h"] > 720]
        if not bad:
            c2.passed(f"{len(ladder)} tiers, max={ladder[-1]['id'] if ladder else 'n/a'}")
        else:
            c2.failed(f"ladder contained entries above cap: {[r['id'] for r in bad]}")
        checks.append(c2)

        # ── check 3: non-admin is forbidden from mutating global policy ──
        print(f"\n── step 4: low user tries admin PATCH (expect 403) ──")
        status = await try_admin_patch(session, base, low_token)
        print(f"  status={status}")

        c3 = Check("non-admin rejected at PATCH /api/admin/media-policy")
        if status == 403:
            c3.passed("403 Forbidden")
        else:
            c3.failed(f"expected 403, got {status}")
        checks.append(c3)

        # ── check 4: unconstrained user still gets 8K in their ladder ──
        print(f"\n── step 5: high user fetches /api/media-policy/me ──")
        high_cap_resp = await get_cap(session, base, high_token)
        high_cap = high_cap_resp["cap"]
        high_ladder = high_cap_resp["ladder"]
        has_8k = any(r.get("id") == "4320p" or r.get("w", 0) >= 7680 for r in high_ladder)
        print(f"  cap: {high_cap['max_width']}x{high_cap['max_height']} src={high_cap['source']}")
        print(f"  has 8K: {has_8k}")

        c4 = Check("unconstrained user still sees 8K")
        if has_8k and high_cap["max_width"] >= 7680:
            c4.passed(f"8K present, cap={high_cap['max_width']}x{high_cap['max_height']}")
        else:
            c4.failed(f"8K missing; cap={high_cap}")
        checks.append(c4)

        # ── cleanup ──
        print(f"\n── step 6: cleanup override ──")
        clear_user_override(db_path, low_id)
        print("  removed")

    # ── Report ──
    print("\n══════════════ REPORT ══════════════")
    for c in checks:
        mark = "✓" if c.ok else "✗"
        print(f"  [{mark}] {c.name} — {c.detail}")
    all_ok = all(c.ok for c in checks)
    print("\nResult:", "PASS ✓" if all_ok else "FAIL ✗")
    return 0 if all_ok else 1


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=3000)
    p.add_argument("--project-root", default=str(Path(__file__).resolve().parent.parent))
    args = p.parse_args()
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
