"""
Apple Push Notification service (APNs) provider — token-based auth (HTTP/2).

Uses the JWT token-based auth flow (recommended over .p12 certificates):
    1. Sign a short-lived ES256 JWT with the .p8 auth key
    2. POST to https://api.push.apple.com/3/device/{token} with Authorization

Configuration via environment variables:
    APNS_AUTH_KEY_PATH  — path to AuthKey_XXXXXX.p8 (or inline PEM)
    APNS_KEY_ID         — Apple Key ID (10 chars)
    APNS_TEAM_ID        — Apple Team ID (10 chars)
    APNS_TOPIC          — bundle id (e.g. com.example.app)
    APNS_USE_SANDBOX    — "1" to use api.sandbox.push.apple.com

Implementation note: HTTP/2 is required by APNs. We use httpx with the
`http2=True` flag (depends on the `h2` package). If h2 is unavailable we
return `apns_h2_unavailable` and the dispatcher silently skips.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import httpx

from app.core.logging import get_logger
from app.services.push.provider import PushPayload, PushResult

logger = get_logger(__name__)


class ApnsProvider:
    name = "apns"

    def __init__(self) -> None:
        self._jwt: str | None = None
        self._jwt_iat: float = 0.0
        self._lock = asyncio.Lock()
        self._client: httpx.AsyncClient | None = None

    @property
    def _topic(self) -> str:
        return os.environ.get("APNS_TOPIC", "")

    @property
    def _use_sandbox(self) -> bool:
        return os.environ.get("APNS_USE_SANDBOX", "0") in ("1", "true", "True")

    @property
    def _base_url(self) -> str:
        return (
            "https://api.sandbox.push.apple.com"
            if self._use_sandbox
            else "https://api.push.apple.com"
        )

    async def is_configured(self) -> bool:
        return bool(
            os.environ.get("APNS_AUTH_KEY_PATH")
            and os.environ.get("APNS_KEY_ID")
            and os.environ.get("APNS_TEAM_ID")
            and self._topic
        )

    def _load_private_key(self) -> str | None:
        raw = os.environ.get("APNS_AUTH_KEY_PATH", "")
        if not raw:
            return None
        if raw.lstrip().startswith("-----BEGIN"):
            return raw
        try:
            return Path(raw).expanduser().read_text(encoding="utf-8")
        except OSError as e:
            logger.warning("apns_key_load_failed", error=str(e))
            return None

    async def _get_jwt(self) -> str | None:
        async with self._lock:
            now = time.time()
            # APNs JWTs must be refreshed at least every hour, max once / 20m
            if self._jwt and now - self._jwt_iat < 1800:
                return self._jwt
            try:
                import jwt  # PyJWT
            except ImportError:
                logger.warning("apns_pyjwt_unavailable")
                return None
            key = self._load_private_key()
            if not key:
                return None
            key_id = os.environ.get("APNS_KEY_ID", "")
            team_id = os.environ.get("APNS_TEAM_ID", "")
            try:
                self._jwt = jwt.encode(
                    {"iss": team_id, "iat": int(now)},
                    key,
                    algorithm="ES256",
                    headers={"alg": "ES256", "kid": key_id},
                )
            except Exception as e:
                logger.warning("apns_jwt_sign_failed", error=str(e))
                return None
            self._jwt_iat = now
            return self._jwt

    async def _get_client(self) -> httpx.AsyncClient | None:
        if self._client is not None:
            return self._client
        try:
            self._client = httpx.AsyncClient(http2=True, timeout=15.0)
        except (ImportError, RuntimeError) as e:
            logger.warning("apns_h2_unavailable", error=str(e))
            return None
        return self._client

    async def send_one(
        self,
        token: str,
        payload: PushPayload,
        *,
        extra: dict | None = None,
    ) -> PushResult:
        if not await self.is_configured():
            return PushResult(success=False, error="apns_not_configured")

        token_jwt = await self._get_jwt()
        if not token_jwt:
            return PushResult(success=False, error="apns_no_jwt")

        client = await self._get_client()
        if client is None:
            return PushResult(success=False, error="apns_h2_unavailable")

        # Build APNs payload
        aps: dict = {
            "alert": {
                "title": payload.title,
                **({"body": payload.body} if payload.body else {}),
            },
        }
        if payload.sound:
            aps["sound"] = payload.sound
        if payload.badge is not None:
            aps["badge"] = payload.badge
        if payload.category:
            aps["category"] = payload.category
        if payload.content_available:
            aps["content-available"] = 1

        body: dict = {"aps": aps}
        for k, v in payload.data.items():
            if k != "aps":
                body[k] = v

        topic = (extra or {}).get("bundle_id") or self._topic
        headers = {
            "authorization": f"bearer {token_jwt}",
            "apns-topic": topic,
            "apns-push-type": "alert" if not payload.content_available else "background",
            "apns-priority": "10" if not payload.content_available else "5",
        }
        if payload.collapse_id:
            headers["apns-collapse-id"] = payload.collapse_id[:64]

        url = f"{self._base_url}/3/device/{token}"
        try:
            resp = await client.post(url, headers=headers, json=body)
        except httpx.HTTPError as e:
            return PushResult(success=False, error=f"apns_http_error:{e}")

        if resp.status_code == 200:
            return PushResult(
                success=True,
                provider_message_id=resp.headers.get("apns-id"),
            )

        # Parse APNs error reasons
        try:
            reason = resp.json().get("reason", "")
        except ValueError:
            reason = resp.text[:120]
        invalid = resp.status_code == 410 or reason in {
            "BadDeviceToken",
            "Unregistered",
            "DeviceTokenNotForTopic",
        }
        return PushResult(
            success=False,
            error=f"apns_status_{resp.status_code}:{reason}",
            invalid_token=invalid,
        )

    async def aclose(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None
