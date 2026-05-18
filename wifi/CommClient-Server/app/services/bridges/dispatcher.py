"""
Bridge dispatcher — runtime supervisor.

Responsibilities:
* Load every enabled :class:`BridgeConfig` from the DB.
* Instantiate the matching ``BridgeProtocol`` and run it under a managed task.
* Subscribe to Helen's internal message stream and fan-out outgoing
  messages to bridges whose ``channel_helen_id`` matches.
* Receive ``BridgeIncoming`` callbacks and persist them as a real Helen
  message via :func:`_post_to_helen` (uses the internal MessageService).
* Restart crashed bridges with exponential backoff.

The dispatcher is a singleton — bootstrap from ``app/main.py`` startup.
"""
from __future__ import annotations

import asyncio
import contextlib
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db_session
from app.core.logging import get_logger
from app.models.bridge import (
    BridgeConfig,
    BridgeIdentity,
    BridgeMessage,
    VALID_BRIDGE_DIRECTIONS,
)

from .base import (
    BridgeHealth,
    BridgeIncoming,
    BridgeOutgoing,
    BridgeProtocol,
    BridgeRegistry,
)

# Pull in all adapters so they register themselves with the registry.
from . import discord_bridge as _d  # noqa: F401
from . import telegram_bridge as _t  # noqa: F401
from . import slack_bridge as _s  # noqa: F401

logger = get_logger(__name__)


_BACKOFF_INITIAL = 2.0
_BACKOFF_MAX = 60.0


class _BridgeRunner:
    """Wraps one BridgeProtocol with restart-on-crash semantics."""

    def __init__(self, dispatcher: "BridgeDispatcher", cfg: BridgeConfig) -> None:
        self._d = dispatcher
        self.cfg = cfg
        self._impl: BridgeProtocol | None = None
        self._task: asyncio.Task[Any] | None = None
        self._stop_evt = asyncio.Event()
        self._backoff = _BACKOFF_INITIAL

    @property
    def impl(self) -> BridgeProtocol | None:
        return self._impl

    async def start(self) -> None:
        self._stop_evt.clear()
        self._task = asyncio.create_task(
            self._supervise(), name=f"bridge-supervisor-{self.cfg.id}",
        )

    async def stop(self) -> None:
        self._stop_evt.set()
        if self._impl is not None:
            with contextlib.suppress(Exception):
                await self._impl.stop()
        if self._task is not None:
            with contextlib.suppress(Exception, asyncio.CancelledError):
                self._task.cancel()
                await self._task
        self._impl = None
        self._task = None

    async def _supervise(self) -> None:
        while not self._stop_evt.is_set():
            try:
                self._impl = BridgeRegistry.create(
                    self.cfg, self._d._on_incoming,
                )
                await self._impl.start()
                await self._d._mark_status(self.cfg.id, "running", None)
                self._backoff = _BACKOFF_INITIAL
                # Block until stop_evt set
                while not self._stop_evt.is_set() and self._impl.running:
                    await asyncio.sleep(5)
            except asyncio.CancelledError:                     # pragma: no cover
                break
            except Exception as exc:
                logger.error("bridge_runner_crash", bridge_id=self.cfg.id,
                             error=str(exc))
                await self._d._mark_status(self.cfg.id, "error", str(exc))
                self._impl = None
                try:
                    await asyncio.wait_for(
                        self._stop_evt.wait(), timeout=self._backoff,
                    )
                    return
                except asyncio.TimeoutError:
                    self._backoff = min(self._backoff * 2, _BACKOFF_MAX)
                    continue
            else:
                if not self._stop_evt.is_set():
                    await self._d._mark_status(self.cfg.id, "stopped", None)


class BridgeDispatcher:
    """Singleton dispatcher."""

    _instance: "BridgeDispatcher | None" = None

    def __init__(self) -> None:
        self._runners: dict[str, _BridgeRunner] = {}
        self._by_channel: dict[str, list[str]] = defaultdict(list)
        self._lock = asyncio.Lock()
        self._started = False

    @classmethod
    def instance(cls) -> "BridgeDispatcher":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ── lifecycle ──────────────────────────────────────────

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        async for db in get_db_session():
            rows = (await db.execute(
                select(BridgeConfig).where(BridgeConfig.enabled.is_(True))
            )).scalars().all()
        for cfg in rows:
            await self._spawn(cfg)
        logger.info("bridge_dispatcher_started", count=len(self._runners))

    async def stop(self) -> None:
        if not self._started:
            return
        async with self._lock:
            for r in list(self._runners.values()):
                await r.stop()
            self._runners.clear()
            self._by_channel.clear()
        self._started = False

    # ── runtime control ────────────────────────────────────

    async def start_bridge(self, cfg: BridgeConfig) -> None:
        async with self._lock:
            if cfg.id in self._runners:
                return
            await self._spawn(cfg)

    async def stop_bridge(self, bridge_id: str) -> None:
        async with self._lock:
            r = self._runners.pop(bridge_id, None)
            if r is None:
                return
            self._by_channel[r.cfg.channel_helen_id] = [
                bid for bid in self._by_channel.get(r.cfg.channel_helen_id, [])
                if bid != bridge_id
            ]
            await r.stop()

    async def reload_bridge(self, cfg: BridgeConfig) -> None:
        await self.stop_bridge(cfg.id)
        if cfg.enabled:
            await self.start_bridge(cfg)

    async def _spawn(self, cfg: BridgeConfig) -> None:
        r = _BridgeRunner(self, cfg)
        self._runners[cfg.id] = r
        self._by_channel[cfg.channel_helen_id].append(cfg.id)
        await r.start()

    # ── fan-out from Helen ─────────────────────────────────

    async def on_helen_message(
        self, *, helen_message_id: str, channel_helen_id: str,
        sender_display: str, sender_avatar: str | None, text: str,
        attachments: list[dict[str, Any]] | None = None,
    ) -> None:
        """Called by the socket.io / message-service hook for every new
        message. Forwards to all bridges configured for that channel."""
        bridge_ids = list(self._by_channel.get(channel_helen_id, ()))
        if not bridge_ids:
            return
        for bid in bridge_ids:
            r = self._runners.get(bid)
            if r is None or r.impl is None:
                continue
            outgoing = BridgeOutgoing(
                config=r.cfg,
                helen_message_id=helen_message_id,
                sender_display=sender_display,
                sender_avatar=sender_avatar,
                text=text,
                attachments=attachments or [],
            )
            asyncio.create_task(
                self._send_one(r, outgoing),
                name=f"bridge-out-{bid}-{helen_message_id}",
            )

    async def _send_one(self, runner: _BridgeRunner, msg: BridgeOutgoing) -> None:
        try:
            remote_id = await runner.impl.send_to_remote(msg)        # type: ignore[union-attr]
            await self._record_message(
                bridge_id=runner.cfg.id,
                helen_message_id=msg.helen_message_id,
                remote_message_id=remote_id,
                direction="helen_to_remote",
                status="sent",
                raw={"text_len": len(msg.text)},
            )
        except Exception as exc:
            logger.error("bridge_send_failed", bridge_id=runner.cfg.id,
                         error=str(exc))
            await self._record_message(
                bridge_id=runner.cfg.id,
                helen_message_id=msg.helen_message_id,
                remote_message_id=None,
                direction="helen_to_remote",
                status="failed",
                raw={"error": str(exc)},
                error=str(exc),
            )

    # ── inbound from bridges ───────────────────────────────

    async def _on_incoming(self, payload: BridgeIncoming) -> None:
        try:
            await self._upsert_identity(payload)
            helen_msg_id = await self._post_to_helen(payload)
            await self._record_message(
                bridge_id=payload.config.id,
                helen_message_id=helen_msg_id,
                remote_message_id=payload.remote_message_id,
                direction="remote_to_helen",
                status="delivered",
                raw=payload.raw,
            )
        except Exception as exc:
            logger.error("bridge_incoming_persist_failed",
                         bridge_id=payload.config.id, error=str(exc))
            await self._record_message(
                bridge_id=payload.config.id,
                helen_message_id=None,
                remote_message_id=payload.remote_message_id,
                direction="remote_to_helen",
                status="failed",
                raw=payload.raw, error=str(exc),
            )

    async def _post_to_helen(self, payload: BridgeIncoming) -> str:
        """Persist incoming bridge message as a real Helen message.
        Uses the message service if available, otherwise raw INSERT."""
        from app.models.message import Message
        import secrets as _sec

        async for db in get_db_session():
            mid = _sec.token_hex(16)
            body = f"[{payload.config.kind}:{payload.remote_username}] {payload.text}"
            m = Message(
                id=mid,
                channel_id=payload.config.channel_helen_id,
                sender_id="bridge:" + payload.config.id,
                content=body,
                created_at=datetime.now(timezone.utc),
            )
            db.add(m)
            await db.commit()

            # Best-effort socket.io broadcast
            try:
                from app.realtime.sio import sio  # type: ignore
                await sio.emit("message:new", {
                    "id": mid,
                    "channel_id": payload.config.channel_helen_id,
                    "sender": "bridge:" + payload.config.kind,
                    "content": body,
                    "via_bridge": payload.config.id,
                })
            except Exception:
                pass

            return mid
        return ""

    async def _upsert_identity(self, payload: BridgeIncoming) -> None:
        async for db in get_db_session():
            existing = (await db.execute(
                select(BridgeIdentity).where(
                    BridgeIdentity.bridge_id == payload.config.id,
                    BridgeIdentity.remote_user_id == payload.remote_user_id,
                )
            )).scalar_one_or_none()
            if existing is None:
                import secrets as _sec
                db.add(BridgeIdentity(
                    id=_sec.token_hex(16),
                    bridge_id=payload.config.id,
                    helen_user_id=None,
                    remote_user_id=payload.remote_user_id,
                    remote_username=payload.remote_username,
                    avatar_url=payload.avatar_url,
                ))
            else:
                existing.remote_username = payload.remote_username
                existing.avatar_url = payload.avatar_url or existing.avatar_url
            await db.commit()

    # ── persistence helpers ────────────────────────────────

    async def _record_message(
        self, *, bridge_id: str, helen_message_id: str | None,
        remote_message_id: str | None, direction: str, status: str,
        raw: dict[str, Any], error: str | None = None,
    ) -> None:
        if direction not in VALID_BRIDGE_DIRECTIONS:
            return
        import secrets as _sec
        async for db in get_db_session():
            db.add(BridgeMessage(
                id=_sec.token_hex(16),
                bridge_id=bridge_id,
                helen_message_id=helen_message_id,
                remote_message_id=remote_message_id,
                direction=direction,
                status=status,
                raw_payload=raw,
                error=error,
            ))
            await db.commit()

    async def _mark_status(
        self, bridge_id: str, status: str, error: str | None,
    ) -> None:
        async for db in get_db_session():
            await db.execute(
                update(BridgeConfig)
                .where(BridgeConfig.id == bridge_id)
                .values(last_status=status, last_error=error,
                        last_health_at=datetime.now(timezone.utc))
            )
            await db.commit()

    # ── health & introspection ─────────────────────────────

    async def health(self, bridge_id: str) -> BridgeHealth | None:
        r = self._runners.get(bridge_id)
        if r is None or r.impl is None:
            return None
        try:
            return await r.impl.health()
        except Exception as e:                                  # pragma: no cover
            return BridgeHealth(False, f"health err: {e}")

    def status_table(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for bid, r in self._runners.items():
            out.append({
                "bridge_id": bid,
                "kind": r.cfg.kind,
                "name": r.cfg.name,
                "running": bool(r.impl and r.impl.running),
                "channel_helen_id": r.cfg.channel_helen_id,
                "channel_remote_id": r.cfg.channel_remote_id,
            })
        return out


dispatcher = BridgeDispatcher.instance()
