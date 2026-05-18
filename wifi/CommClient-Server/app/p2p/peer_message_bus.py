"""P2P message bus — local pub/sub for inbound peer messages.

Inbound peer messages (after federation HMAC verify) are emitted on
this bus by the API layer. Application code subscribes to typed
channels.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from typing import Any, Callable

from app.core.logging import get_logger

logger = get_logger(__name__)

MessageHandler = Callable[[str, dict], None]


class PeerMessageBus:
    _singleton: "PeerMessageBus | None" = None

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._subs: dict[str, list[MessageHandler]] = defaultdict(list)
        self._delivered = 0

    @classmethod
    def instance(cls) -> "PeerMessageBus":
        if cls._singleton is None:
            cls._singleton = PeerMessageBus()
        return cls._singleton

    def subscribe(self, channel: str, handler: MessageHandler) -> None:
        with self._lock:
            self._subs[channel].append(handler)

    def unsubscribe(self, channel: str, handler: MessageHandler) -> None:
        with self._lock:
            try:
                self._subs[channel].remove(handler)
            except ValueError:
                pass

    def deliver(self, channel: str, payload: dict) -> int:
        with self._lock:
            handlers = list(self._subs.get(channel, []))
            wildcard = list(self._subs.get("*", []))
            self._delivered += 1
        for h in handlers + wildcard:
            try:
                h(channel, payload)
            except Exception as e:
                logger.warning("peer_msg_handler_failed",
                               channel=channel, error=str(e))
        return len(handlers) + len(wildcard)

    def stats(self) -> dict:
        with self._lock:
            return {
                "delivered_total": self._delivered,
                "channels":        sorted(self._subs.keys()),
                "subscribers":     {ch: len(hs) for ch, hs in self._subs.items()},
            }


def get_message_bus() -> PeerMessageBus:
    return PeerMessageBus.instance()
