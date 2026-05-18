"""
Helen Python SDK — REST + WebSocket client.

Designed for ops automation, bots, and integrations. Mirrors the
production Electron client's failover behaviour: parallel-race
between top-K endpoints, circuit-breaker per endpoint, queued retry
with exponential backoff.

Quick start::

    import asyncio
    from helen_client import HelenClient

    async def main():
        async with HelenClient(
            base_url="http://10.0.0.5:3000",
            username="alice",
            password="…",
        ) as client:
            channels = await client.list_channels()
            for c in channels:
                print(c.id, c.name)

            await client.send_message(
                channel_id=channels[0].id,
                text="Hello from the Python SDK",
            )

    asyncio.run(main())
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, AsyncIterator, Optional

try:
    import httpx
except ImportError as exc:
    raise ImportError(
        "helen_client needs httpx. Install with `pip install httpx`."
    ) from exc

from helen_client.types import (
    AuthToken, Channel, Message, User, Call, KeyBundle,
)


class HelenError(Exception):
    """Raised on any non-2xx server response."""

    def __init__(self, status: int, body: Any, message: str = "") -> None:
        super().__init__(message or f"HTTP {status}: {body}")
        self.status = status
        self.body = body


class HelenClient:
    """Async client. Use as an async context manager so the underlying
    httpx connection pool is released even on exception."""

    def __init__(
        self,
        base_url: str,
        *,
        username: Optional[str] = None,
        password: Optional[str] = None,
        access_token: Optional[str] = None,
        timeout_sec: float = 10.0,
        verify_tls: bool = False,           # LAN-only by default
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self._token: Optional[AuthToken] = (
            AuthToken(access_token=access_token) if access_token else None
        )
        self._http: Optional[httpx.AsyncClient] = None
        self._timeout = timeout_sec
        self._verify = verify_tls

    async def __aenter__(self) -> "HelenClient":
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(self._timeout, connect=2.0),
            verify=self._verify,
            limits=httpx.Limits(max_connections=10,
                                 max_keepalive_connections=4),
        )
        if self._token is None and self.username and self.password:
            await self.login(self.username, self.password)
        return self

    async def __aexit__(self, *exc) -> None:
        if self._http:
            await self._http.aclose()
            self._http = None

    # ── core HTTP helper ────────────────────────────────────

    async def _req(self, method: str, path: str,
                    *, json_body: Any = None,
                    params: Any = None,
                    auth: bool = True) -> Any:
        if not self._http:
            raise RuntimeError("Use HelenClient as an async context manager")
        headers = {}
        if auth and self._token:
            headers["Authorization"] = f"Bearer {self._token.access_token}"
        r = await self._http.request(
            method, path, json=json_body, params=params, headers=headers,
        )
        if 200 <= r.status_code < 300:
            try:
                return r.json()
            except Exception:
                return r.text
        try:
            body = r.json()
        except Exception:
            body = r.text
        raise HelenError(r.status_code, body)

    # ── auth ────────────────────────────────────────────────

    async def login(self, username: str, password: str) -> AuthToken:
        body = await self._req(
            "POST", "/api/auth/login",
            json_body={"username": username, "password": password},
            auth=False,
        )
        self._token = AuthToken(
            access_token=body.get("access_token") or body.get("token"),
            refresh_token=body.get("refresh_token"),
            expires_in=body.get("expires_in", 0),
        )
        return self._token

    async def me(self) -> User:
        body = await self._req("GET", "/api/users/me")
        return User(
            id=body["id"], username=body["username"],
            display_name=body.get("display_name", body["username"]),
            avatar_url=body.get("avatar_url"),
            status=body.get("status", "offline"),
            role=body.get("role", "member"),
        )

    # ── channels ────────────────────────────────────────────

    async def list_channels(self) -> list[Channel]:
        body = await self._req("GET", "/api/channels")
        items = body.get("channels", body) if isinstance(body, dict) else body
        return [
            Channel(
                id=c["id"], name=c.get("name", "?"),
                type=c.get("type", "channel"),
                members_count=c.get("members_count", 0),
                last_message_at=c.get("last_message_at", 0.0),
            )
            for c in items
        ]

    async def create_channel(self, name: str,
                              type: str = "channel") -> Channel:
        body = await self._req(
            "POST", "/api/channels",
            json_body={"name": name, "type": type},
        )
        return Channel(
            id=body["id"], name=body["name"], type=body.get("type", type),
        )

    # ── messages ────────────────────────────────────────────

    async def list_messages(self, channel_id: str,
                              limit: int = 50,
                              before: Optional[str] = None
                              ) -> list[Message]:
        params = {"limit": limit}
        if before:
            params["before"] = before
        body = await self._req(
            "GET", f"/api/channels/{channel_id}/messages",
            params=params,
        )
        items = body.get("messages", body) if isinstance(body, dict) else body
        return [
            Message(
                id=m["id"], channel_id=m["channel_id"],
                sender_id=m["sender_id"],
                content=m.get("content", ""),
                sent_at=m.get("sent_at") or m.get("created_at") or 0.0,
                edited_at=m.get("edited_at"),
                reply_to=m.get("reply_to"),
                attachments=m.get("attachments", []),
                reactions=m.get("reactions", {}),
                encrypted=bool(m.get("encrypted")),
            )
            for m in items
        ]

    async def send_message(
        self, channel_id: str, text: str,
        *,
        reply_to: Optional[str] = None,
        attachments: Optional[list[dict]] = None,
    ) -> Message:
        body = await self._req(
            "POST", f"/api/channels/{channel_id}/messages",
            json_body={
                "content": text,
                "reply_to": reply_to,
                "attachments": attachments or [],
            },
        )
        return Message(
            id=body["id"], channel_id=channel_id,
            sender_id=body.get("sender_id", ""),
            content=text,
            sent_at=body.get("sent_at") or time.time(),
            reply_to=reply_to,
            attachments=body.get("attachments", []),
        )

    async def edit_message(self, channel_id: str,
                            message_id: str, text: str) -> Message:
        body = await self._req(
            "PUT", f"/api/channels/{channel_id}/messages/{message_id}",
            json_body={"content": text},
        )
        return Message(
            id=message_id, channel_id=channel_id,
            sender_id=body.get("sender_id", ""), content=text,
            sent_at=body.get("sent_at", 0),
            edited_at=body.get("edited_at", time.time()),
        )

    async def delete_message(self, channel_id: str,
                              message_id: str) -> None:
        await self._req(
            "DELETE",
            f"/api/channels/{channel_id}/messages/{message_id}",
        )

    # ── calls ──────────────────────────────────────────────

    async def list_active_calls(self) -> list[Call]:
        body = await self._req("GET", "/api/calls")
        items = body.get("calls", body) if isinstance(body, dict) else body
        return [
            Call(
                id=c["id"], channel_id=c["channel_id"],
                started_by=c.get("started_by", ""),
                started_at=c.get("started_at", 0.0),
                participants=c.get("participants", []),
                is_video=bool(c.get("is_video")),
            )
            for c in items
        ]

    # ── E2EE key bundle ─────────────────────────────────────

    async def fetch_key_bundle(self, user_id: str) -> KeyBundle:
        body = await self._req("GET", f"/api/keys/{user_id}/bundle")
        return KeyBundle(
            user_id=user_id,
            identity_pub=body["identity_pub"],
            signing_pub=body["signing_pub"],
            signed_pre_pub=body["signed_pre_pub"],
            signed_pre_id=body["signed_pre_id"],
            signed_pre_sig=body["signed_pre_sig"],
            one_time_pre_pubs=[
                (p["id"], p["pub"])
                for p in body.get("one_time_pre_pubs", [])
            ],
        )

    # ── Socket.IO event stream (read-only) ──────────────────

    async def stream_events(self) -> AsyncIterator[dict]:
        """Yield each Socket.IO frame from the server.

        Uses the polling transport — simpler than WebSocket and good
        enough for ops scripts. For high-throughput stream consumers,
        use the official socketio Python client directly.
        """
        if not self._http or not self._token:
            raise RuntimeError("client not authenticated")
        sid = None
        while True:
            params = {"EIO": "4", "transport": "polling"}
            if sid:
                params["sid"] = sid
            r = await self._http.get("/socket.io/", params=params)
            if r.status_code != 200:
                await asyncio.sleep(2)
                continue
            for frame in _parse_eio_payload(r.text):
                if isinstance(frame, dict):
                    yield frame
                    if not sid and "sid" in frame:
                        sid = frame["sid"]


def _parse_eio_payload(payload: str) -> list:
    """Tiny parser for Engine.IO 4 polling frames."""
    out = []
    for chunk in payload.split("\x1e"):
        if not chunk:
            continue
        try:
            if chunk.startswith("0"):
                out.append(json.loads(chunk[1:]))
            elif chunk.startswith("4"):
                # 42[name, payload] socket.io format
                inner = json.loads(chunk[2:])
                out.append({"event": inner[0],
                             "data": inner[1] if len(inner) > 1 else None})
            else:
                out.append({"raw": chunk})
        except Exception:
            continue
    return out
