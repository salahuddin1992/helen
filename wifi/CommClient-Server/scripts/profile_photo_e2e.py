"""
End-to-end smoke test for the profile-photos feature.

Registers two users, uploads photos with different visibility levels, and
verifies who can see what.

Run while the server is live on http://localhost:3000.
"""

from __future__ import annotations

import io
import sys
import uuid

import requests

BASE = "http://localhost:3000"


def fail(msg: str) -> None:
    print(f"FAIL: {msg}")
    sys.exit(1)


def ok(msg: str) -> None:
    print(f"OK   {msg}")


def png_bytes(color: tuple[int, int, int]) -> bytes:
    """Tiny 1x1 PNG."""
    import struct
    import zlib

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack("!I", len(data))
            + tag
            + data
            + struct.pack("!I", zlib.crc32(tag + data))
        )

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack("!IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    raw = b"\x00" + bytes(color)
    idat = zlib.compress(raw)
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def register(username: str, display_name: str, password: str) -> dict:
    r = requests.post(
        f"{BASE}/api/auth/register",
        json={"username": username, "display_name": display_name, "password": password},
        timeout=5,
    )
    if r.status_code not in (200, 201):
        # Username taken from a previous run — log in instead
        r2 = requests.post(
            f"{BASE}/api/auth/login",
            json={"username": username, "password": password, "device_name": "pytest"},
            timeout=5,
        )
        r2.raise_for_status()
        return r2.json()
    return r.json()


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def main() -> None:
    suffix = uuid.uuid4().hex[:6]
    u1 = register(f"photo_owner_{suffix}", "Owner", "pw123456!A")
    u2 = register(f"photo_viewer_{suffix}", "Viewer", "pw123456!A")

    t1 = u1["tokens"]["access_token"]
    t2 = u2["tokens"]["access_token"]
    id1 = u1["user"]["id"]
    id2 = u2["user"]["id"]

    ok("registered two users")

    # 1. Upload three photos with different visibility
    uploads = []
    for visibility, color in (
        ("public", (255, 0, 0)),
        ("contacts", (0, 255, 0)),
        ("private", (0, 0, 255)),
    ):
        files = {"file": (f"{visibility}.png", io.BytesIO(png_bytes(color)), "image/png")}
        data = {"visibility": visibility, "make_primary": "false"}
        r = requests.post(
            f"{BASE}/api/users/me/photos",
            headers=auth(t1),
            files=files,
            data=data,
            timeout=10,
        )
        if r.status_code != 201:
            fail(f"upload {visibility}: {r.status_code} {r.text}")
        uploads.append(r.json())
    ok(f"uploaded 3 photos (public/contacts/private)")

    # 2. Owner sees all 3
    r = requests.get(f"{BASE}/api/users/me/photos", headers=auth(t1), timeout=5)
    if r.status_code != 200:
        fail(f"list own: {r.status_code}")
    own = r.json()
    if own["total"] != 3:
        fail(f"owner should see 3, got {own['total']}")
    ok("owner lists 3 photos")

    # 3. Viewer (not in contacts) — only public is visible
    r = requests.get(f"{BASE}/api/users/{id1}/photos", headers=auth(t2), timeout=5)
    if r.status_code != 200:
        fail(f"list as viewer: {r.status_code}")
    vis = r.json()
    if vis["total"] != 1:
        fail(f"viewer (not contact) should see 1 (public), got {vis['total']}")
    if vis["photos"][0]["visibility"] != "public":
        fail(f"viewer should see only public, got {vis['photos'][0]['visibility']}")
    ok("non-contact viewer sees only public photo")

    # 4. Owner adds viewer as a contact — now contacts photo becomes visible
    r = requests.post(
        f"{BASE}/api/users/me/contacts",
        headers=auth(t1),
        json={"contact_id": id2},
        timeout=5,
    )
    if r.status_code not in (200, 201, 409):
        fail(f"add contact: {r.status_code} {r.text}")
    r = requests.get(f"{BASE}/api/users/{id1}/photos", headers=auth(t2), timeout=5)
    vis2 = r.json()
    if vis2["total"] != 2:
        fail(f"contact viewer should see 2 (public+contacts), got {vis2['total']}")
    seen = sorted(p["visibility"] for p in vis2["photos"])
    if seen != ["contacts", "public"]:
        fail(f"contact viewer sees unexpected set: {seen}")
    ok("contact viewer sees public + contacts (not private)")

    # 5. Private photo 403s on image endpoint for viewer
    private_photo = next(p for p in own["photos"] if p["visibility"] == "private")
    r = requests.get(
        f"{BASE}/api/users/{id1}/photos/{private_photo['id']}/image",
        headers=auth(t2),
        timeout=5,
    )
    if r.status_code != 403:
        fail(f"private image should return 403 to non-owner, got {r.status_code}")
    ok("private photo binary returns 403 to non-owner")

    # 6. Owner can fetch the private binary
    r = requests.get(
        f"{BASE}/api/users/{id1}/photos/{private_photo['id']}/image",
        headers=auth(t1),
        timeout=5,
    )
    if r.status_code != 200:
        fail(f"owner should fetch own private image, got {r.status_code}")
    if not r.content.startswith(b"\x89PNG"):
        fail("fetched image did not match PNG signature")
    ok("owner can fetch own private image bytes")

    # 7. Set contacts photo as primary — avatar_url should update
    contacts_photo = next(p for p in own["photos"] if p["visibility"] == "contacts")
    r = requests.patch(
        f"{BASE}/api/users/me/photos/{contacts_photo['id']}",
        headers=auth(t1),
        json={"is_primary": True},
        timeout=5,
    )
    if r.status_code != 200:
        fail(f"set primary: {r.status_code} {r.text}")
    me = requests.get(f"{BASE}/api/users/me", headers=auth(t1), timeout=5).json()
    if me["avatar_url"] != f"/api/users/{id1}/photos/{contacts_photo['id']}/image":
        fail(f"avatar_url should mirror new primary, got {me['avatar_url']}")
    ok("set-primary mirrors into user.avatar_url")

    # 8. Delete the primary — server auto-promotes another photo
    r = requests.delete(
        f"{BASE}/api/users/me/photos/{contacts_photo['id']}",
        headers=auth(t1),
        timeout=5,
    )
    if r.status_code != 204:
        fail(f"delete: {r.status_code}")
    remaining = requests.get(
        f"{BASE}/api/users/me/photos", headers=auth(t1), timeout=5
    ).json()
    if remaining["total"] != 2:
        fail(f"after delete expected 2, got {remaining['total']}")
    primaries = [p for p in remaining["photos"] if p["is_primary"]]
    if len(primaries) != 1:
        fail(f"exactly 1 primary expected after auto-promote, got {len(primaries)}")
    ok("deleting primary auto-promotes another photo")

    # 9. Cleanup
    for p in remaining["photos"]:
        requests.delete(
            f"{BASE}/api/users/me/photos/{p['id']}", headers=auth(t1), timeout=5
        )
    ok("cleanup complete")

    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
