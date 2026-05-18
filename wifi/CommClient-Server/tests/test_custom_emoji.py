"""Tests for the custom-emoji REST + service layers.

Two layers of coverage:

  * Service layer — shortcode validation, mime allow-list, size cap,
    duplicate refusal, delete cleans up disk + metadata.

  * REST endpoints — list/upload/delete/raw-fetch + admin RBAC.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from httpx import AsyncClient

from app.services.custom_emoji_service import (
    CustomEmojiError,
    upload_emoji,
    list_emoji,
    delete_emoji,
    get_emoji,
    get_emoji_path,
)


# ── Service layer ──────────────────────────────────────────────


# A 1×1 transparent PNG — valid bytes the service accepts as-is.
_PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c63000100000005000172e7b3160000000049454e44"
    "ae426082"
)


@pytest.fixture
def cleanup_emoji():
    """Drop any emoji left behind by previous tests so the per-test
    state is predictable. Runs both before and after each test."""
    for e in list_emoji():
        delete_emoji(e.id)
    yield
    for e in list_emoji():
        delete_emoji(e.id)


class TestCustomEmojiService:

    def test_upload_returns_stable_metadata(self, cleanup_emoji):
        e = upload_emoji(
            shortcode="hello",
            mime="image/png",
            body_bytes=_PNG_1X1,
            uploaded_by="user-test",
            description="hi",
        )
        assert e.shortcode == "hello"
        assert e.mime == "image/png"
        assert e.size_bytes == len(_PNG_1X1)
        assert get_emoji(e.id) is not None
        path = get_emoji_path(e.id)
        assert path is not None and path.is_file()

    def test_shortcode_lowercased(self, cleanup_emoji):
        e = upload_emoji(
            shortcode="HelloWorld_1",
            mime="image/png",
            body_bytes=_PNG_1X1,
            uploaded_by="u",
        )
        assert e.shortcode == "helloworld_1"

    def test_shortcode_validation(self, cleanup_emoji):
        bad_codes = [
            "",            # empty
            "a",           # 1 char (must be ≥2)
            "a" * 33,      # too long
            "_starts_with_underscore",
            "has space",
            "has.dot",
            "has@symbol",
        ]
        for sc in bad_codes:
            with pytest.raises(CustomEmojiError):
                upload_emoji(
                    shortcode=sc,
                    mime="image/png",
                    body_bytes=_PNG_1X1,
                    uploaded_by="u",
                )

    def test_mime_allowlist(self, cleanup_emoji):
        with pytest.raises(CustomEmojiError):
            upload_emoji(
                shortcode="evil",
                mime="application/x-msdownload",
                body_bytes=b"MZ\x00",
                uploaded_by="u",
            )

    def test_size_cap(self, cleanup_emoji, monkeypatch):
        # 1 KiB cap for this test.
        monkeypatch.setenv("HELEN_CUSTOM_EMOJI_MAX_BYTES", "1024")
        big = b"x" * 2000
        with pytest.raises(CustomEmojiError):
            upload_emoji(
                shortcode="toobig",
                mime="image/png",
                body_bytes=big,
                uploaded_by="u",
            )

    def test_duplicate_shortcode_refused(self, cleanup_emoji):
        upload_emoji(
            shortcode="dup",
            mime="image/png",
            body_bytes=_PNG_1X1,
            uploaded_by="u",
        )
        with pytest.raises(CustomEmojiError):
            upload_emoji(
                shortcode="dup",
                mime="image/png",
                body_bytes=_PNG_1X1,
                uploaded_by="u",
            )

    def test_delete_removes_file_and_metadata(self, cleanup_emoji):
        e = upload_emoji(
            shortcode="bye",
            mime="image/png",
            body_bytes=_PNG_1X1,
            uploaded_by="u",
        )
        path = get_emoji_path(e.id)
        assert path is not None and path.is_file()
        assert delete_emoji(e.id) is True
        assert get_emoji(e.id) is None
        assert not path.exists()

    def test_delete_unknown_returns_false(self, cleanup_emoji):
        assert delete_emoji("does-not-exist-1234") is False

    def test_list_sorted_by_shortcode(self, cleanup_emoji):
        for sc in ["zeta", "alpha", "mike"]:
            upload_emoji(
                shortcode=sc,
                mime="image/png",
                body_bytes=_PNG_1X1,
                uploaded_by="u",
            )
        names = [e.shortcode for e in list_emoji()]
        assert names == sorted(names)


# ── REST endpoints ─────────────────────────────────────────────


class TestCustomEmojiAPI:

    async def test_listing_is_public_for_authed(
        self,
        client: AsyncClient,
        auth_headers: dict,
        cleanup_emoji,
    ):
        res = await client.get(
            "/api/custom-emoji",
            headers=auth_headers,
        )
        assert res.status_code == 200
        assert "emoji" in res.json()

    async def test_listing_requires_auth(
        self,
        client: AsyncClient,
        cleanup_emoji,
    ):
        # Helen's auth dep rejects unauthenticated requests with
        # either 401 or 403 depending on whether the header is
        # missing vs present-but-invalid; both signal "you're not
        # logged in" to the client.
        res = await client.get("/api/custom-emoji")
        assert res.status_code in (401, 403)

    async def test_upload_requires_admin_role(
        self,
        client: AsyncClient,
        auth_headers: dict,
        second_user_headers: dict,
        cleanup_emoji,
    ):
        # The default user (auth_headers) is created with role=user
        # by the conftest fixture; second_user is the same. The
        # require_role("admin") dep should reject both.
        files = {
            "file": ("e.png", io.BytesIO(_PNG_1X1), "image/png"),
        }
        data = {"shortcode": "regular", "description": ""}
        res = await client.post(
            "/api/custom-emoji",
            files=files, data=data,
            headers=second_user_headers,
        )
        assert res.status_code in (401, 403)

    async def test_admin_upload_then_list(
        self,
        client: AsyncClient,
        admin_headers: dict,
        cleanup_emoji,
    ):
        files = {
            "file": ("e.png", io.BytesIO(_PNG_1X1), "image/png"),
        }
        data = {
            "shortcode": "thumbsup-internal",
            "description": "test",
        }
        res = await client.post(
            "/api/custom-emoji",
            files=files, data=data,
            headers=admin_headers,
        )
        assert res.status_code == 201
        body = res.json()
        emoji_id = body["id"]

        # The new emoji shows up in the listing.
        listing = await client.get(
            "/api/custom-emoji",
            headers=admin_headers,
        )
        ids = [e["id"] for e in listing.json()["emoji"]]
        assert emoji_id in ids

        # The raw bytes endpoint serves the file.
        raw = await client.get(
            f"/api/custom-emoji/{emoji_id}/raw",
            headers=admin_headers,
        )
        assert raw.status_code == 200
        assert raw.content == _PNG_1X1

    async def test_upload_rejects_invalid_shortcode(
        self,
        client: AsyncClient,
        admin_headers: dict,
        cleanup_emoji,
    ):
        files = {
            "file": ("e.png", io.BytesIO(_PNG_1X1), "image/png"),
        }
        data = {"shortcode": "has space", "description": ""}
        res = await client.post(
            "/api/custom-emoji",
            files=files, data=data,
            headers=admin_headers,
        )
        assert res.status_code == 400

    async def test_admin_delete_returns_204(
        self,
        client: AsyncClient,
        admin_headers: dict,
        cleanup_emoji,
    ):
        # Upload → delete → 404 on raw fetch.
        files = {
            "file": ("e.png", io.BytesIO(_PNG_1X1), "image/png"),
        }
        data = {"shortcode": "to-delete", "description": ""}
        res = await client.post(
            "/api/custom-emoji",
            files=files, data=data,
            headers=admin_headers,
        )
        emoji_id = res.json()["id"]

        del_res = await client.delete(
            f"/api/custom-emoji/{emoji_id}",
            headers=admin_headers,
        )
        assert del_res.status_code == 204

        raw = await client.get(
            f"/api/custom-emoji/{emoji_id}/raw",
            headers=admin_headers,
        )
        assert raw.status_code == 404

    async def test_delete_unknown_404(
        self,
        client: AsyncClient,
        admin_headers: dict,
        cleanup_emoji,
    ):
        res = await client.delete(
            "/api/custom-emoji/does-not-exist",
            headers=admin_headers,
        )
        assert res.status_code == 404
