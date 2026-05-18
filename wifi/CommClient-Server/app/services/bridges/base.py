"""
Bridge protocol ABC + plugin registry.

Each bridge implementation (Discord / Telegram / Slack) registers itself via
:func:`BridgeRegistry.register` and is discovered by ``kind``. The dispatcher
spawns one ``BridgeProtocol`` task per enabled ``BridgeConfig`` row.
"""
from __future__ import annotations

import abc
import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, ClassVar

from app.core.logging import get_logger
from app.models.bridge import BridgeConfig

logger = get_logger(__name__)


@dataclass
class BridgeOutgoing:
    """Helen → remote payload."""
    config: BridgeConfig
    helen_message_id: str
    sender_display: str
    sender_avatar: str | None
    text: str
    attachments: list[dict[str, Any]] = field(default_factory=list)
    reply_to_remote_id: str | None = None


@dataclass
class BridgeIncoming:
    """Remote → Helen payload."""
    config: BridgeConfig
    remote_message_id: str
    remote_user_id: str
    remote_username: str
    avatar_url: str | None
    text: str
    attachments: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class BridgeHealth:
    ok: bool
    detail: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


# Callback type the dispatcher injects so adapters can post into Helen.
IncomingCallback = Callable[[BridgeIncoming], Awaitable[None]]


class BridgeProtocol(abc.ABC):
    """Base class for every bridge adapter."""

    kind: ClassVar[str] = ""

    def __init__(self, config: BridgeConfig, on_incoming: IncomingCallback) -> None:
        self.config = config
        self._on_incoming = on_incoming
        self._running = False
        self._task: asyncio.Task[Any] | None = None

    @property
    def running(self) -> bool:
        return self._running

    @abc.abstractmethod
    async def start(self) -> None: ...

    @abc.abstractmethod
    async def stop(self) -> None: ...

    @abc.abstractmethod
    async def send_to_remote(self, msg: BridgeOutgoing) -> str:
        """Returns the remote-message-id assigned by the platform."""

    @abc.abstractmethod
    async def health(self) -> BridgeHealth: ...

    async def _emit(self, payload: BridgeIncoming) -> None:
        try:
            await self._on_incoming(payload)
        except Exception as exc:                                # pragma: no cover
            logger.error(
                "bridge_incoming_dispatch_error",
                kind=self.kind, bridge_id=self.config.id, error=str(exc),
            )


class BridgeRegistry:
    """Plugin discovery — adapters register a factory keyed by their ``kind``."""

    _factories: ClassVar[dict[str, type[BridgeProtocol]]] = {}

    @classmethod
    def register(cls, factory: type[BridgeProtocol]) -> type[BridgeProtocol]:
        if not factory.kind:
            raise ValueError(f"{factory.__name__} missing kind classvar")
        cls._factories[factory.kind] = factory
        logger.info("bridge_registered", kind=factory.kind, cls=factory.__name__)
        return factory

    @classmethod
    def create(
        cls, config: BridgeConfig, on_incoming: IncomingCallback,
    ) -> BridgeProtocol:
        try:
            f = cls._factories[config.kind]
        except KeyError as e:
            raise ValueError(f"unknown bridge kind: {config.kind!r}") from e
        return f(config, on_incoming)

    @classmethod
    def list_kinds(cls) -> list[str]:
        return sorted(cls._factories.keys())
