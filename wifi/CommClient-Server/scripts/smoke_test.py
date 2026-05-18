"""
End-to-end smoke test for the CommClient-Server.

Exercises all 6 new features (tasks #65-#70):
  - drafts
  - message edit history
  - channel categories
  - user schedule / away
  - message templates
  - granular permissions

Run against a server started with `python run.py` (PORT=3007).
"""

from __future__ import annotations

import json
import random
import string
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:3007"


# ── HTTP helpers ────────────────────────────────────────────


def _rand(n: int = 6) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


def _req(method: str, path: str, token: str | None = None, body: dict | None = None) -> tuple[int, dict]:
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode() or "{}"
            return resp.status, json.loads(raw) if raw.strip().startswith(("{", "[")) else {"raw": raw}
    except urllib.error.HTTPError as e:
        raw = e.read().decode() if e.fp else ""
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw}


# ── Assertions ───────────────────────────────────────────────


OK = 0
FAIL = 0
LINES: list[str] = []


def check(label: str, cond: bool, extra: str = "") -> None:
    global OK, FAIL
    if cond:
        OK += 1
        LINES.append(f"  OK  {label}")
    else:
        FAIL += 1
        LINES.append(f"  XX  {label}  {extra}")


def section(title: str) -> None:
    LINES.append("")
    LINES.append(f"-- {title} --")


# ── Register + login ────────────────────────────────────────


def register(username: str) -> tuple[str, str]:
    code, body = _req(
        "POST",
        "/api/auth/register",
        body={"username": username, "password": "Passw0rd!" + _rand(), "display_name": username},
    )
    assert code in (200, 201), f"register {username} failed: {code} {body}"
    token = body.get("access_token") or body.get("token") or body.get("tokens", {}).get("access_token")
    uid = body.get("user", {}).get("id") or body.get("id") or body.get("user_id")
    if token is None or uid is None:
        # some auth variants return {user: {...}, access_token}
        token = token or body.get("access_token")
        uid = uid or (body.get("user") or {}).get("id")
    assert token and uid, f"no token/uid in register response: {body}"
    return uid, token


# ── Test body ────────────────────────────────────────────────


def run() -> int:
    suffix = _rand()
    alice = f"e2e_a_{suffix}"
    bob = f"e2e_b_{suffix}"

    section("auth")
    alice_id, alice_tok = register(alice)
    bob_id, bob_tok = register(bob)
    check("register alice", bool(alice_id and alice_tok))
    check("register bob", bool(bob_id and bob_tok))

    # Create a group channel with both
    section("channel setup")
    code, body = _req(
        "POST",
        "/api/channels",
        token=alice_tok,
        body={"type": "group", "name": f"smoke_{suffix}", "member_ids": [bob_id]},
    )
    check("create group", code in (200, 201), f"{code} {body}")
    group_id = body.get("id") or body.get("channel", {}).get("id")
    check("group id present", bool(group_id), str(body)[:200])
    if not group_id:
        LINES.append("  (stopping — no group id)")
        return 1

    # Post a message so we have something to edit
    code, body = _req(
        "POST",
        f"/api/channels/{group_id}/messages",
        token=alice_tok,
        body={"content": "hello v1", "message_type": "text"},
    )
    check("post message", code in (200, 201), f"{code} {body}")
    msg_id = body.get("id") or body.get("message", {}).get("id")

    # ── Feature: Drafts ────────────────────────────────────
    section("drafts (#65)")
    code, body = _req(
        "PUT",
        "/api/drafts",
        token=alice_tok,
        body={"channel_id": group_id, "content": "draft text v1"},
    )
    check("upsert draft", code in (200, 201), f"{code} {body}")

    code, body = _req(
        "PUT",
        "/api/drafts",
        token=alice_tok,
        body={"channel_id": group_id, "content": "draft text v2"},
    )
    check("upsert draft (update)", code in (200, 201) and (body.get("content") == "draft text v2"), str(body)[:200])

    code, body = _req("GET", "/api/drafts", token=alice_tok)
    items = body.get("items") if isinstance(body, dict) else body
    check(
        "list drafts",
        code == 200 and any(d.get("content") == "draft text v2" for d in (items or [])),
        str(body)[:200],
    )

    code, body = _req("GET", f"/api/drafts/by-channel?channel_id={group_id}", token=alice_tok)
    draft = body.get("draft") if isinstance(body, dict) else None
    check(
        "get draft by channel",
        code == 200 and (draft or {}).get("content") == "draft text v2",
        str(body)[:200],
    )

    code, body = _req(
        "DELETE",
        f"/api/drafts/by-channel?channel_id={group_id}",
        token=alice_tok,
    )
    check("delete draft", code in (200, 204), f"{code} {body}")

    # ── Feature: Edit history ──────────────────────────────
    section("edit history (#66)")
    if msg_id:
        code, body = _req(
            "PATCH",
            f"/api/messages/{msg_id}",
            token=alice_tok,
            body={"content": "hello v2"},
        )
        check("edit message v2", code == 200, f"{code} {body}")

        code, body = _req(
            "PATCH",
            f"/api/messages/{msg_id}",
            token=alice_tok,
            body={"content": "hello v3"},
        )
        check("edit message v3", code == 200, f"{code} {body}")

        code, body = _req("GET", f"/api/messages/{msg_id}/history", token=alice_tok)
        hist = body if isinstance(body, list) else body.get("history", [])
        check(
            "edit history has 2 snapshots",
            code == 200 and len(hist) >= 2,
            f"{code} len={len(hist)} body={str(body)[:200]}",
        )

    # ── Feature: Channel categories ────────────────────────
    section("channel categories (#67)")
    code, body = _req(
        "POST",
        "/api/channel-categories",
        token=alice_tok,
        body={"name": f"Work_{suffix}", "color": "#ff0000"},
    )
    check("create category", code in (200, 201), f"{code} {body}")
    cat_id = body.get("id")

    if cat_id:
        code, body = _req(
            "POST",
            f"/api/channel-categories/{cat_id}/channels",
            token=alice_tok,
            body={"channel_id": group_id},
        )
        check("assign channel to category", code in (200, 201), f"{code} {body}")

        code, body = _req("GET", "/api/channel-categories", token=alice_tok)
        cats = body.get("items") if isinstance(body, dict) else body
        check("list categories", code == 200 and any(c.get("id") == cat_id for c in (cats or [])), str(body)[:200])

        code, body = _req(
            "PATCH",
            f"/api/channel-categories/{cat_id}",
            token=alice_tok,
            body={"name": f"Work2_{suffix}", "is_collapsed": True},
        )
        check("patch category", code == 200, f"{code} {body}")

    # ── Feature: Schedule / away ───────────────────────────
    section("schedule + away (#68)")
    code, body = _req(
        "POST",
        "/api/schedule/rules",
        token=alice_tok,
        body={
            "weekday": 1,
            "start_minute": 9 * 60,
            "end_minute": 17 * 60,
            "status": "available",
            "label": "Work hours",
        },
    )
    check("add schedule rule", code in (200, 201), f"{code} {body}")

    code, body = _req("GET", "/api/schedule/rules", token=alice_tok)
    rules = body.get("items") if isinstance(body, dict) else body
    check("list schedule rules", code == 200 and len(rules or []) >= 1, str(body)[:200])

    code, body = _req(
        "PUT",
        "/api/schedule/away",
        token=alice_tok,
        body={"text": "Out of office", "is_active": True, "mode": "always_away"},
    )
    check("set away message", code in (200, 201), f"{code} {body}")

    code, body = _req("GET", "/api/schedule/me/status", token=alice_tok)
    check(
        "get my status",
        code == 200 and body.get("mode") in ("always_away", "schedule", "always_on"),
        str(body)[:200],
    )

    # ── Feature: Templates ─────────────────────────────────
    section("templates (#69)")
    code, body = _req(
        "POST",
        "/api/templates",
        token=alice_tok,
        body={
            "shortcut": f"hi_{suffix}",
            "title": "Greeting",
            "content": "Hello there!",
            "scope": "personal",
        },
    )
    check("create template", code in (200, 201), f"{code} {body}")

    code, body = _req("GET", f"/api/templates/resolve?shortcut=hi_{suffix}", token=alice_tok)
    resolved = body.get("resolved") if isinstance(body, dict) else None
    check(
        "resolve template",
        code == 200 and (resolved or body or {}).get("content") == "Hello there!",
        str(body)[:200],
    )

    code, body = _req("GET", "/api/templates", token=alice_tok)
    tmpls = body.get("items") if isinstance(body, dict) else body
    check("list templates", code == 200 and len(tmpls or []) >= 1, str(body)[:200])

    # duplicate rejection
    code, body = _req(
        "POST",
        "/api/templates",
        token=alice_tok,
        body={
            "shortcut": f"hi_{suffix}",
            "title": "Dup",
            "content": "dup",
            "scope": "personal",
        },
    )
    check("duplicate template rejected", code in (400, 409), f"{code} {body}")

    # ── Feature: Granular permissions ──────────────────────
    section("permissions (#70)")
    code, body = _req("GET", f"/api/channels/{group_id}/permissions/me", token=bob_tok)
    eff = (body or {}).get("effective", {})
    check(
        "bob effective has 'post'=true by default",
        code == 200 and eff.get("post") is True,
        str(body)[:200],
    )
    check(
        "bob effective has 'kick'=false by default",
        eff.get("kick") is False,
        str(eff)[:200],
    )

    # alice grants 'pin' to member role
    code, body = _req(
        "PUT",
        f"/api/channels/{group_id}/permissions/role",
        token=alice_tok,
        body={"role": "member", "permission": "pin", "granted": True},
    )
    check("grant member->pin role perm", code in (200, 201), f"{code} {body}")

    code, body = _req("GET", f"/api/channels/{group_id}/permissions/me", token=bob_tok)
    eff = (body or {}).get("effective", {})
    check("bob now has pin via role", eff.get("pin") is True, str(eff)[:200])

    # alice revokes bob's 'pin' via member override
    code, body = _req(
        "PUT",
        f"/api/channels/{group_id}/permissions/member",
        token=alice_tok,
        body={"user_id": bob_id, "permission": "pin", "granted": False},
    )
    check("set member override", code in (200, 201), f"{code} {body}")

    code, body = _req("GET", f"/api/channels/{group_id}/permissions/me", token=bob_tok)
    eff = (body or {}).get("effective", {})
    check("member override beats role grant", eff.get("pin") is False, str(eff)[:200])

    # bob can't set role perms (not manage_roles)
    code, body = _req(
        "PUT",
        f"/api/channels/{group_id}/permissions/role",
        token=bob_tok,
        body={"role": "member", "permission": "kick", "granted": True},
    )
    check("bob blocked from set_role_permission", code == 403, f"{code} {body}")

    return 0


if __name__ == "__main__":
    t0 = time.time()
    try:
        run()
    except Exception as e:
        LINES.append(f"  FATAL: {e!r}")
        FAIL += 1
    elapsed = time.time() - t0
    print("\n".join(LINES))
    print()
    print(f"passed={OK}  failed={FAIL}  time={elapsed:.2f}s")
    sys.exit(0 if FAIL == 0 else 1)
