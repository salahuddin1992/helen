"""
Firebase Cloud Messaging (FCM) HTTP v1 provider.

Uses the modern v1 endpoint:
    POST https://fcm.googleapis.com/v1/projects/{project_id}/messages:send

Authentication is a short-lived OAuth2 access token derived from a Google
service account JSON file (the standard FCM credential format). The token
is cached and refreshed lazily.

The implementation is intentionally dependency-light: only `httpx` and the
stdlib are required. JWT signing for the OAuth2 assertion uses PyJWT
(already a project dependency for app auth tokens).

If `FCM_CREDENTIALS_JSON` is not set, `is_configured()` returns False and
the dispatcher silently skips this provider.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from app.core.logging import get_logger
from app.services.push.provider import PushPayload, PushResult

logger = get_logger(__name__)


_FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
_FCM_TOKEN_URI = "https://oauth2.googleapis.com/token"
_FCM_SEND_URL = "https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"


@dataclass
class _ServiceAccount:
    project_id: str
    client_email: str
    private_key: str
    token_uri: str = _FCM_TOKEN_URI


class FcmProvider:
    name = "fcm"

    def __init__(self) -> None:
        self._sa: _ServiceAccount | None = None
        self._access_token: str | None = None
        self._access_token_exp: float = 0.0
        self._lock = asyncio.Lock()
        self._load_attempted = False

    # ── Credential loading ────────────────────────────────────

    def _load_service_account(self) -> _ServiceAccount | None:
        """Load credentials from FCM_CREDENTIALS_JSON (path or inline JSON)."""
        raw = os.environ.get("FCM_CREDENTIALS_JSON")
        if not raw:
            return None
        try:
            if raw.strip().startswith("{"):
                data = json.loads(raw)
            else:
                p = Path(raw).expanduser()
                if not p.exists():
                    logger.warning("fcm_credentials_path_missing", path=str(p))
                    return None
                data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("fcm_credentials_parse_failed", error=str(e))
            return None
        try:
            return _ServiceAccount(
                project_id=data["project_id"],
                client_email=data["client_email"],
                private_key=data["private_key"],
                token_uri=data.get("token_uri", _FCM_TOKEN_URI),
            )
        except KeyError as e:
            logger.warning("fcm_credentials_missing_field", field=str(e))
            return None

    async def is_configured(self) -> bool:
        if self._sa is not None:
            return True
        if self._load_attempted:
            return False
        self._load_attempted = True
        self._sa = self._load_service_account()
        return self._sa is not None

    # ── OAuth2 token mint (JWT bearer) ────────────────────────

    async def _get_access_token(self) -> str | None:
        async with self._lock:
            now = time.time()
            if self._access_token and now < self._access_token_exp - 60:
                return self._access_token
            if not await self.is_configured():
                return None
            sa = self._sa
            assert sa is not None
            try:
                import jwt  # PyJWT
            except ImportError:
                logger.warning("fcm_pyjwt_unavailable")
                return None
            iat = int(now)
            exp = iat + 3600
            assertion = jwt.encode(
                {
                    "iss": sa.client_email,
                    "scope": _FCM_SCOPE,
                    "aud": sa.token_uri,
                    "iat": iat,
                    "exp": exp,
                },
                sa.private_key,
                algorithm="RS256",
            )
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    resp = await client.post(
                        sa.token_uri,
                        data={
                            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                            "assertion": assertion,
                        },
                    )
                if resp.status_code != 200:
                    logger.warning(
                        "fcm_token_exchange_failed",
                        status=resp.status_code,
                        body=resp.text[:200],
                    )
                    return None
                data = resp.json()
            except httpx.HTTPError as e:
                logger.warning("fcm_token_exchange_error", error=str(e))
                return None
            self._access_token = data.get("access_token")
            self._access_token_exp = now + int(data.get("expires_in", 3600))
            return self._access_token

    # ── Send ──────────────────────────────────────────────────

    async def send_one(
        self,
        token: str,
        payload: PushPayload,
        *,
        extra: dict | None = None,
    ) -> PushResult:
        if not await self.is_configured():
            return PushResult(success=False, error="fcm_not_configured")

        access_token = await self._get_access_token()
        if not access_token:
            return PushResult(success=False, error="fcm_no_access_token")

        sa = self._sa
        assert sa is not None
        url = _FCM_SEND_URL.format(project_id=sa.project_id)

        message: dict = {
            "token": token,
            "notification": {"title": payload.title},
        }
        if payload.body:
            message["notification"]["body"] = payload.body
        if payload.data:
            # FCM v1 requires string values in the data dict
            message["data"] = {k: str(v) for k, v in payload.data.items()}
        if payload.collapse_id:
            message.setdefault("android", {})["collapse_key"] = payload.collapse_id
        if payload.content_available:
            message.setdefault("apns", {}).setdefault("payload", {}).setdefault(
                "aps", {}
            )["content-available"] = 1
        if payload.sound:
            message.setdefault("android", {}).setdefault("notification", {})[
                "sound"
            ] = payload.sound

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Content-Type": "application/json; charset=UTF-8",
                    },
                    json={"message": message},
                )
        except httpx.HTTPError as e:
            return PushResult(success=False, error=f"fcm_http_error:{e}")

        if 200 <= resp.status_code < 300:
            try:
                msg_id = resp.json().get("name")
            except (ValueError, AttributeError):
                msg_id = None
            return PushResult(success=True, provider_message_id=msg_id)

        body = resp.text[:200]
        # 404 / 400 with UNREGISTERED / INVALID_ARGUMENT → invalid token
        invalid = False
        if resp.status_code in (404, 410):
            invalid = True
        elif resp.status_code == 400 and (
            "UNREGISTERED" in body or "INVALID_ARGUMENT" in body or "registration-token-not-registered" in body
        ):
            invalid = True
        return PushResult(
            success=False,
            error=f"fcm_status_{resp.status_code}:{body}",
            invalid_token=invalid,
        )
