"""
Slack bridge adapter.

* Outbound: Web API ``chat.postMessage`` via ``slack-sdk``.
* Inbound : Socket Mode (default) — requires ``app_token`` + ``bot_token``.

Settings (``BridgeConfig.settings``):
    bot_token       : str  — xoxb-…
    app_token       : str  — xapp-…    (Socket Mode)
    username_prefix : str  — default "[Helen]"
"""
from __future__ import annotations

import asyncio
from collections import deque
from typing import Any

from app.core.logging import get_logger

from .base import (
    BridgeHealth,
    BridgeIncoming,
    BridgeOutgoing,
    BridgeProtocol,
    BridgeRegistry,
    IncomingCallback,
)
from .sanitizer import FormatAdapter, looks_like_loop

logger = get_logger(__name__)


try:
    from slack_sdk.web.async_client import AsyncWebClient    # type: ignore
    from slack_sdk.socket_mode.aiohttp import SocketModeClient   # type: ignore
    from slack_sdk.socket_mode.response import SocketModeResponse   # type: ignore
    from slack_sdk.socket_mode.request import SocketModeRequest    # type: ignore
    _SLACK_OK = True
except Exception:                                            # pragma: no cover
    AsyncWebClient = None
    SocketModeClient = None
    SocketModeResponse = None
    SocketModeRequest = None
    _SLACK_OK = False


class SlackBridgeUnavailable(RuntimeError):
    pass


@BridgeRegistry.register
class SlackBridge(BridgeProtocol):
    kind = "slack"

    def __init__(self, config, on_incoming: IncomingCallback) -> None:
        super().__init__(config, on_incoming)
        self._web: Any = None
        self._socket: Any = None
        self._fmt = FormatAdapter("slack")
        self._recent_out: deque[str] = deque(maxlen=32)
        self._socket_task: asyncio.Task[Any] | None = None

    async def start(self) -> None:
        if self._running:
            return
        if not _SLACK_OK:
            raise SlackBridgeUnavailable(
                "slack-sdk not installed (pip install slack-sdk)"
            )
        s = self.config.settings or {}
        bot_token = s.get("bot_token")
        app_token = s.get("app_token")
        if not bot_token:
            raise SlackBridgeUnavailable("bot_token required")

        self._web = AsyncWebClient(token=bot_token)

        if app_token:
            self._socket = SocketModeClient(
                app_token=app_token, web_client=self._web,
            )

            target = self.config.channel_remote_id

            async def _process(client: "SocketModeClient",
                               req: "SocketModeRequest") -> None:
                ack = SocketModeResponse(envelope_id=req.envelope_id)
                await client.send_socket_mode_response(ack)
                if req.type != "events_api":
                    return
                ev = req.payload.get("event", {})
                if ev.get("type") != "message":
                    return
                if ev.get("subtype") in ("bot_message", "message_changed",
                                         "message_deleted"):
                    return
                if ev.get("channel") != target:
                    return
                text = ev.get("text", "") or ""
                if looks_like_loop(text, self._recent_out):
                    return
                user_id = ev.get("user", "0")
                username = "slack"
                try:
                    info = await self._web.users_info(user=user_id)
                    username = info["user"]["profile"].get(
                        "display_name") or info["user"].get("real_name") or user_id
                except Exception:
                    pass
                atts: list[dict[str, Any]] = []
                for f in ev.get("files", []) or []:
                    atts.append({
                        "url": f.get("url_private_download") or f.get("url_private"),
                        "filename": f.get("name"),
                        "content_type": f.get("mimetype"),
                        "size": f.get("size"),
                    })
                payload = BridgeIncoming(
                    config=self.config,
                    remote_message_id=ev.get("ts", ""),
                    remote_user_id=user_id,
                    remote_username=username,
                    avatar_url=None,
                    text=self._fmt.remote_to_helen(text),
                    attachments=atts,
                    raw={"channel": ev.get("channel"),
                         "team": req.payload.get("team_id")},
                )
                await self._emit(payload)

            self._socket.socket_mode_request_listeners.append(_process)
            await self._socket.connect()

        self._running = True
        logger.info("slack_bridge_started", bridge_id=self.config.id,
                    socket=bool(app_token))

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        try:
            if self._socket is not None:
                await self._socket.close()
        finally:
            self._socket = None
            self._web = None

    async def send_to_remote(self, msg: BridgeOutgoing) -> str:
        if not self._running or self._web is None:
            raise SlackBridgeUnavailable("not running")
        s = self.config.settings or {}
        prefix = f"[{s.get('username_prefix','Helen')}:{msg.sender_display}]"
        payload = self._fmt.helen_to_remote(msg.text, prefix)
        body_text = payload["text"] if isinstance(payload, dict) else str(payload)
        blocks = payload["blocks"] if isinstance(payload, dict) else None
        self._recent_out.append(body_text)

        resp = await self._web.chat_postMessage(
            channel=self.config.channel_remote_id,
            text=body_text,
            blocks=blocks,
            username=prefix.strip("[]"),
        )
        return str(resp.get("ts"))

    async def health(self) -> BridgeHealth:
        if not _SLACK_OK:
            return BridgeHealth(False, "library missing")
        if not self._running:
            return BridgeHealth(False, "not running")
        try:
            test = await self._web.auth_test()
            return BridgeHealth(True, "ok", {"team": test.get("team"),
                                             "user": test.get("user")})
        except Exception as e:                                  # pragma: no cover
            return BridgeHealth(False, f"auth_test failed: {e}")
