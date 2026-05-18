"""
Discord bridge adapter.

* Primary path: ``discord.py`` Gateway client (full event stream, edits, deletes).
* Fallback path: ``discord-webhook`` outbound-only if Gateway can't be used.
* Both are optional dependencies — the module imports lazily and degrades to
  ``DiscordBridgeUnavailable`` if neither is installed.

Settings (``BridgeConfig.settings``):
    bot_token        : str   — required for Gateway
    guild_id         : str   — optional but recommended
    webhook_url      : str   — fallback / outbound-only
    username_prefix  : str   — default "[Helen]"
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
    import discord  # type: ignore
    _DISCORD_PY = True
except Exception:                                              # pragma: no cover
    discord = None
    _DISCORD_PY = False

try:
    from discord_webhook import DiscordWebhook  # type: ignore
    _DISCORD_WEBHOOK = True
except Exception:                                              # pragma: no cover
    DiscordWebhook = None
    _DISCORD_WEBHOOK = False


class DiscordBridgeUnavailable(RuntimeError):
    pass


@BridgeRegistry.register
class DiscordBridge(BridgeProtocol):
    kind = "discord"

    def __init__(self, config, on_incoming: IncomingCallback) -> None:
        super().__init__(config, on_incoming)
        self._client: Any = None
        self._fmt = FormatAdapter("discord")
        self._recent_out: deque[str] = deque(maxlen=32)
        self._gateway_task: asyncio.Task[Any] | None = None
        self._ready_evt = asyncio.Event()

    # ── lifecycle ──────────────────────────────────────────

    async def start(self) -> None:
        if self._running:
            return
        s = self.config.settings or {}
        token = s.get("bot_token")
        webhook = s.get("webhook_url")

        if not token and not webhook:
            raise DiscordBridgeUnavailable(
                "discord bridge needs either bot_token or webhook_url"
            )

        if token and _DISCORD_PY:
            await self._start_gateway(token)
        elif webhook and _DISCORD_WEBHOOK:
            logger.info("discord_webhook_only_mode", bridge_id=self.config.id)
            self._running = True
        else:
            raise DiscordBridgeUnavailable(
                "no usable discord client lib installed "
                "(pip install discord.py discord-webhook)"
            )

    async def _start_gateway(self, token: str) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        client = discord.Client(intents=intents)
        target_channel = int(self.config.channel_remote_id)

        @client.event
        async def on_ready() -> None:                          # noqa: E306
            self._ready_evt.set()
            logger.info("discord_gateway_ready",
                        user=str(client.user), bridge_id=self.config.id)

        @client.event
        async def on_message(message: "discord.Message") -> None:   # noqa: E306
            if message.author.bot:
                return
            if message.channel.id != target_channel:
                return
            if looks_like_loop(message.content, self._recent_out):
                return
            atts: list[dict[str, Any]] = [
                {"url": a.url, "filename": a.filename,
                 "content_type": a.content_type, "size": a.size}
                for a in message.attachments
            ]
            payload = BridgeIncoming(
                config=self.config,
                remote_message_id=str(message.id),
                remote_user_id=str(message.author.id),
                remote_username=str(message.author.display_name),
                avatar_url=(str(message.author.display_avatar.url)
                            if message.author.display_avatar else None),
                text=self._fmt.remote_to_helen(message.content),
                attachments=atts,
                raw={"channel_id": message.channel.id,
                     "guild_id": getattr(message.guild, "id", None)},
            )
            await self._emit(payload)

        @client.event
        async def on_message_edit(_before, after: "discord.Message") -> None:   # noqa: E306, ARG001
            if after.author.bot or after.channel.id != target_channel:
                return
            payload = BridgeIncoming(
                config=self.config,
                remote_message_id=str(after.id),
                remote_user_id=str(after.author.id),
                remote_username=str(after.author.display_name),
                avatar_url=None,
                text="[edited] " + self._fmt.remote_to_helen(after.content),
                raw={"edited": True},
            )
            await self._emit(payload)

        self._client = client
        self._gateway_task = asyncio.create_task(
            client.start(token),
            name=f"discord-bridge-{self.config.id}",
        )
        self._running = True
        try:
            await asyncio.wait_for(self._ready_evt.wait(), timeout=20)
        except asyncio.TimeoutError:
            logger.warning("discord_gateway_handshake_timeout",
                           bridge_id=self.config.id)

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:                                  # pragma: no cover
                pass
        if self._gateway_task is not None:
            self._gateway_task.cancel()
            try:
                await self._gateway_task
            except (asyncio.CancelledError, Exception):
                pass
        self._client = None
        self._gateway_task = None
        self._ready_evt.clear()

    # ── transfer ───────────────────────────────────────────

    async def send_to_remote(self, msg: BridgeOutgoing) -> str:
        s = self.config.settings or {}
        prefix = f"[{s.get('username_prefix','Helen')}:{msg.sender_display}]"
        body = self._fmt.helen_to_remote(msg.text, prefix)
        self._recent_out.append(body if isinstance(body, str) else "")

        # Prefer Gateway send (allows edits/deletes/replies).
        if self._client is not None:
            chan = self._client.get_channel(int(self.config.channel_remote_id))
            if chan is None:
                chan = await self._client.fetch_channel(int(self.config.channel_remote_id))
            sent = await chan.send(content=body)
            return str(sent.id)

        # Fallback webhook send.
        if _DISCORD_WEBHOOK and s.get("webhook_url"):
            def _post() -> str:
                wh = DiscordWebhook(url=s["webhook_url"], content=body,
                                    username=prefix.strip("[]"))
                resp = wh.execute()
                rid = ""
                try:
                    rid = str(resp.json().get("id"))  # type: ignore[union-attr]
                except Exception:
                    rid = ""
                return rid or "webhook"
            return await asyncio.to_thread(_post)

        raise DiscordBridgeUnavailable("no discord transport configured")

    async def health(self) -> BridgeHealth:
        if not _DISCORD_PY and not _DISCORD_WEBHOOK:
            return BridgeHealth(False, "discord libs missing")
        if not self._running:
            return BridgeHealth(False, "not running")
        if self._client is not None:
            return BridgeHealth(
                ok=self._client.is_ready(),
                detail="gateway",
                extra={"latency_ms": int((self._client.latency or 0) * 1000)},
            )
        return BridgeHealth(True, "webhook-only")
