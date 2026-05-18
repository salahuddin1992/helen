"""
Telegram bridge adapter — ``python-telegram-bot`` based.

Settings (``BridgeConfig.settings``):
    bot_token       : str  — required
    chat_id         : str  — required (telegram chat / supergroup / channel id)
    username_prefix : str  — default "[Helen]"
    mode            : "polling" (default) | "webhook"
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
    from telegram import Update                              # type: ignore
    from telegram.ext import (                               # type: ignore
        Application, ApplicationBuilder, MessageHandler, filters,
        ContextTypes,
    )
    _TG_OK = True
except Exception:                                            # pragma: no cover
    Update = None
    Application = None
    ApplicationBuilder = None
    MessageHandler = None
    filters = None
    ContextTypes = None
    _TG_OK = False


class TelegramBridgeUnavailable(RuntimeError):
    pass


@BridgeRegistry.register
class TelegramBridge(BridgeProtocol):
    kind = "telegram"

    def __init__(self, config, on_incoming: IncomingCallback) -> None:
        super().__init__(config, on_incoming)
        self._app: Any = None
        self._fmt = FormatAdapter("telegram")
        self._recent_out: deque[str] = deque(maxlen=32)
        self._runner: asyncio.Task[Any] | None = None

    async def start(self) -> None:
        if self._running:
            return
        if not _TG_OK:
            raise TelegramBridgeUnavailable(
                "python-telegram-bot not installed (pip install python-telegram-bot)"
            )
        s = self.config.settings or {}
        token = s.get("bot_token")
        chat_id = s.get("chat_id") or self.config.channel_remote_id
        if not token or not chat_id:
            raise TelegramBridgeUnavailable("bot_token and chat_id required")

        app = ApplicationBuilder().token(token).build()

        async def _on_message(update: "Update", _ctx: "ContextTypes.DEFAULT_TYPE") -> None:
            msg = update.effective_message
            if msg is None:
                return
            if str(update.effective_chat.id) != str(chat_id):
                return
            user = update.effective_user
            text = msg.text or msg.caption or ""
            if looks_like_loop(text, self._recent_out):
                return
            atts: list[dict[str, Any]] = []
            if msg.document is not None:
                atts.append({
                    "file_id": msg.document.file_id,
                    "filename": msg.document.file_name,
                    "size": msg.document.file_size,
                })
            for ph in (msg.photo or [])[-1:]:                  # largest only
                atts.append({"file_id": ph.file_id, "size": ph.file_size,
                             "kind": "photo"})

            payload = BridgeIncoming(
                config=self.config,
                remote_message_id=str(msg.message_id),
                remote_user_id=str(user.id) if user else "0",
                remote_username=(user.full_name if user else "telegram"),
                avatar_url=None,
                text=self._fmt.remote_to_helen(text),
                attachments=atts,
                raw={"chat_id": update.effective_chat.id},
            )
            await self._emit(payload)

        app.add_handler(MessageHandler(filters.ALL, _on_message))

        await app.initialize()
        await app.start()

        if (s.get("mode") or "polling") == "polling":
            await app.updater.start_polling(drop_pending_updates=True)

        self._app = app
        self._running = True
        logger.info("telegram_bridge_started", bridge_id=self.config.id)

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        try:
            if self._app is not None:
                if getattr(self._app, "updater", None) is not None:
                    try:
                        await self._app.updater.stop()
                    except Exception:                          # pragma: no cover
                        pass
                await self._app.stop()
                await self._app.shutdown()
        finally:
            self._app = None

    async def send_to_remote(self, msg: BridgeOutgoing) -> str:
        if not self._running or self._app is None:
            raise TelegramBridgeUnavailable("not running")
        s = self.config.settings or {}
        prefix = f"[{s.get('username_prefix','Helen')}:{msg.sender_display}]"
        body = self._fmt.helen_to_remote(msg.text, prefix)
        self._recent_out.append(body if isinstance(body, str) else "")
        chat_id = s.get("chat_id") or self.config.channel_remote_id
        sent = await self._app.bot.send_message(
            chat_id=chat_id, text=body, parse_mode="MarkdownV2",
        )
        return str(sent.message_id)

    async def health(self) -> BridgeHealth:
        if not _TG_OK:
            return BridgeHealth(False, "library missing")
        if not self._running:
            return BridgeHealth(False, "not running")
        try:
            me = await self._app.bot.get_me()
            return BridgeHealth(True, "ok", {"bot_username": me.username})
        except Exception as e:                                  # pragma: no cover
            return BridgeHealth(False, f"get_me failed: {e}")
