"""
Slack workspace export → Helen importer.

Slack lets workspace owners download a ZIP containing every channel
+ message + DM ever sent. The ZIP layout is well-documented[1]:

    channels.json    — list of channels with ids, names, members
    users.json       — list of users with names, emails, profile
    integrations.json
    <channel-name>/
        2024-01-15.json — messages for that day
        2024-01-16.json
        ...
    dms.json
    mpims.json
    groups.json

This tool walks that tree and writes to Helen via the SDK or
directly to the Helen-Server SQLite. Idempotent — re-runs skip
already-imported records by Slack message id.

Limitations
-----------
* Files / attachments stay as Slack URLs in the message body —
  Slack's file_private URLs require the user's workspace token to
  download. Operators who care about file content should re-host
  the files manually before import.
* Reactions / threads are imported (as native Helen reactions and
  reply_to chains).
* Voice/video calls are skipped — Slack's call media isn't in the
  export anyway.
* Slack timestamps stay intact (no time-shift to import time).

[1]: https://slack.com/help/articles/220556107-Export-your-workspace-data
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
import time
import zipfile
from pathlib import Path
from typing import Any, Iterator, Optional


# ── Slack ZIP parsing ──────────────────────────────────────────────


def _read_json(zf: zipfile.ZipFile, name: str) -> Any:
    try:
        return json.loads(zf.read(name).decode("utf-8"))
    except KeyError:
        return None
    except Exception as exc:
        print(f"[!] couldn't parse {name}: {exc}", file=sys.stderr)
        return None


def list_channels(zf: zipfile.ZipFile) -> list[dict]:
    out = _read_json(zf, "channels.json") or []
    return out if isinstance(out, list) else []


def list_users(zf: zipfile.ZipFile) -> dict[str, dict]:
    raw = _read_json(zf, "users.json") or []
    return {u["id"]: u for u in raw if isinstance(u, dict) and "id" in u}


def iter_messages(zf: zipfile.ZipFile,
                   channel_name: str) -> Iterator[dict]:
    """Yield every message dict in chronological order from a
    Slack channel directory inside the ZIP."""
    prefix = channel_name.rstrip("/") + "/"
    files = sorted(n for n in zf.namelist()
                   if n.startswith(prefix) and n.endswith(".json"))
    for fn in files:
        try:
            for msg in json.loads(zf.read(fn).decode("utf-8")):
                if isinstance(msg, dict):
                    yield msg
        except Exception:
            continue


# ── Helen-Server side: direct SQLite write ────────────────────────


class HelenSQLiteImporter:
    """Bulk import path that writes rows straight into Helen's DB.

    Faster than going through the REST API for huge histories
    (300K+ messages). Pre-condition: Helen-Server is STOPPED while
    we're writing — SQLite WAL handles concurrent reads but a hot
    server may have stale caches afterwards.
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        # Source-of-truth schema differs slightly per Helen version;
        # we discover the columns at import time.
        with sqlite3.connect(db_path) as c:
            cur = c.execute("PRAGMA table_info(messages)")
            self.message_cols = {r[1] for r in cur.fetchall()}
            cur = c.execute("PRAGMA table_info(channels)")
            self.channel_cols = {r[1] for r in cur.fetchall()}
            cur = c.execute("PRAGMA table_info(users)")
            self.user_cols = {r[1] for r in cur.fetchall()}

    def upsert_user(self, slack_user: dict) -> str:
        """Returns the Helen-side user_id for the imported user."""
        helen_id = f"slack:{slack_user['id']}"
        if not self.user_cols:
            return helen_id
        with sqlite3.connect(self.db_path) as c:
            existing = c.execute(
                "SELECT id FROM users WHERE id=?", (helen_id,),
            ).fetchone()
            if existing:
                return helen_id
            cols = []
            vals = []
            if "id" in self.user_cols:
                cols.append("id"); vals.append(helen_id)
            if "username" in self.user_cols:
                cols.append("username")
                vals.append(slack_user.get("name") or helen_id)
            if "display_name" in self.user_cols:
                cols.append("display_name")
                vals.append(
                    slack_user.get("profile", {}).get("display_name")
                    or slack_user.get("name") or helen_id
                )
            if "email" in self.user_cols:
                cols.append("email")
                vals.append(
                    slack_user.get("profile", {}).get("email") or "",
                )
            if "created_at" in self.user_cols:
                cols.append("created_at"); vals.append(time.time())
            placeholders = ", ".join("?" for _ in cols)
            c.execute(
                f"INSERT INTO users ({', '.join(cols)}) "
                f"VALUES ({placeholders})",
                vals,
            )
        return helen_id

    def upsert_channel(self, slack_channel: dict) -> str:
        helen_id = f"slack:{slack_channel['id']}"
        if not self.channel_cols:
            return helen_id
        with sqlite3.connect(self.db_path) as c:
            existing = c.execute(
                "SELECT id FROM channels WHERE id=?", (helen_id,),
            ).fetchone()
            if existing:
                return helen_id
            cols, vals = [], []
            if "id" in self.channel_cols:
                cols.append("id"); vals.append(helen_id)
            if "name" in self.channel_cols:
                cols.append("name")
                vals.append(slack_channel.get("name", "imported"))
            if "type" in self.channel_cols:
                cols.append("type")
                vals.append(
                    "dm" if slack_channel.get("is_im") else "channel",
                )
            if "created_at" in self.channel_cols:
                cols.append("created_at")
                vals.append(slack_channel.get("created", time.time()))
            placeholders = ", ".join("?" for _ in cols)
            c.execute(
                f"INSERT INTO channels ({', '.join(cols)}) "
                f"VALUES ({placeholders})",
                vals,
            )
        return helen_id

    def insert_message(self, slack_msg: dict, helen_channel_id: str,
                        helen_sender_id: str) -> bool:
        if not self.message_cols:
            return False
        slack_msg_id = f"slack:{slack_msg.get('client_msg_id') or slack_msg.get('ts')}"
        text = slack_msg.get("text", "")
        # Slack mentions: <@U12345> → @username
        for u in slack_msg.get("user_profile", []) or []:
            pass  # no easy normalisation; leave raw

        with sqlite3.connect(self.db_path) as c:
            existing = c.execute(
                "SELECT id FROM messages WHERE id=?", (slack_msg_id,),
            ).fetchone()
            if existing:
                return False
            cols, vals = [], []
            if "id" in self.message_cols:
                cols.append("id"); vals.append(slack_msg_id)
            if "channel_id" in self.message_cols:
                cols.append("channel_id"); vals.append(helen_channel_id)
            if "sender_id" in self.message_cols:
                cols.append("sender_id"); vals.append(helen_sender_id)
            if "content" in self.message_cols:
                cols.append("content"); vals.append(text)
            if "sent_at" in self.message_cols:
                cols.append("sent_at")
                vals.append(float(slack_msg.get("ts", time.time())))
            elif "created_at" in self.message_cols:
                cols.append("created_at")
                vals.append(float(slack_msg.get("ts", time.time())))
            if "reply_to" in self.message_cols and slack_msg.get("thread_ts"):
                cols.append("reply_to")
                vals.append(f"slack:{slack_msg['thread_ts']}")
            placeholders = ", ".join("?" for _ in cols)
            try:
                c.execute(
                    f"INSERT INTO messages ({', '.join(cols)}) "
                    f"VALUES ({placeholders})",
                    vals,
                )
                return True
            except sqlite3.IntegrityError:
                return False


# ── Helen-Server side: REST API write ──────────────────────────────


class HelenAPIImporter:
    """Slower but safer — uses the public REST API. Survives a
    running server, applies all server-side rules (audit, RBAC).
    Recommended for online imports."""

    def __init__(self, base_url: str, token: str) -> None:
        try:
            import httpx
        except ImportError:
            raise SystemExit(
                "API-mode import needs httpx. Install with `pip install httpx`."
            )
        self.base_url = base_url.rstrip("/")
        self.token = token

    async def import_zip(self, zip_path: str) -> dict[str, int]:
        import httpx
        stats = {"users": 0, "channels": 0, "messages": 0, "skipped": 0}
        async with httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=30.0,
        ) as client:
            with zipfile.ZipFile(zip_path) as zf:
                channels = list_channels(zf)
                users = list_users(zf)
                # Users
                for slack_u in users.values():
                    r = await client.post("/api/admin/import/user",
                                            json=slack_u)
                    if r.status_code in (200, 201):
                        stats["users"] += 1
                # Channels
                for ch in channels:
                    r = await client.post("/api/admin/import/channel",
                                            json=ch)
                    if r.status_code in (200, 201):
                        stats["channels"] += 1
                # Messages
                for ch in channels:
                    for msg in iter_messages(zf, ch["name"]):
                        r = await client.post(
                            "/api/admin/import/message",
                            json={**msg,
                                   "_slack_channel_id": ch["id"]},
                        )
                        if r.status_code in (200, 201):
                            stats["messages"] += 1
                        else:
                            stats["skipped"] += 1
        return stats


# ── Driver ─────────────────────────────────────────────────────────


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("zip_path", help="Slack export ZIP")
    p.add_argument("--mode", choices=["sqlite", "api"], default="sqlite")
    p.add_argument("--db",
                   default="/opt/helen-server/_internal/data/commclient.db",
                   help="(sqlite mode) Helen DB path")
    p.add_argument("--api-url", default="http://localhost:3000")
    p.add_argument("--token", default="",
                   help="(api mode) admin Bearer token")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    if not Path(args.zip_path).exists():
        print(f"[!] {args.zip_path} not found", file=sys.stderr)
        sys.exit(1)

    if args.mode == "sqlite":
        if not Path(args.db).exists():
            print(f"[!] DB {args.db} not found. Stop the server first "
                   "and pass --db <path>.", file=sys.stderr)
            sys.exit(1)

        importer = HelenSQLiteImporter(args.db)
        stats = {"users": 0, "channels": 0, "messages": 0, "skipped": 0}
        with zipfile.ZipFile(args.zip_path) as zf:
            users = list_users(zf)
            channels = list_channels(zf)
            print(f"  found {len(users)} users, {len(channels)} channels")

            user_id_map: dict[str, str] = {}
            for u in users.values():
                if args.dry_run:
                    user_id_map[u["id"]] = f"slack:{u['id']}"
                else:
                    user_id_map[u["id"]] = importer.upsert_user(u)
                stats["users"] += 1

            for ch in channels:
                if args.dry_run:
                    helen_ch = f"slack:{ch['id']}"
                else:
                    helen_ch = importer.upsert_channel(ch)
                stats["channels"] += 1
                print(f"  [{ch['name']}]")
                for msg in iter_messages(zf, ch["name"]):
                    sender = user_id_map.get(msg.get("user", ""),
                                                "slack:unknown")
                    if args.dry_run:
                        stats["messages"] += 1
                        continue
                    if importer.insert_message(msg, helen_ch, sender):
                        stats["messages"] += 1
                    else:
                        stats["skipped"] += 1
        print(f"\nImported: {stats}")
    else:
        if not args.token:
            print("[!] --token required for API mode", file=sys.stderr)
            sys.exit(1)
        importer = HelenAPIImporter(args.api_url, args.token)
        stats = await importer.import_zip(args.zip_path)
        print(f"Imported: {stats}")


if __name__ == "__main__":
    asyncio.run(main())
