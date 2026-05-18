"""Route-events bus — fire-and-forget hooks for observability.

Strategies emit events via ``emit("route.selected", payload)``; any
number of handlers can subscribe. Handlers run synchronously in the
same task — they're expected to be fast (audit log, metric counter).

Async or expensive handlers should ``asyncio.create_task`` themselves.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from typing import Any, Callable

from app.core.logging import get_logger

logger = get_logger(__name__)


# Event handler signature: (event_name, payload) → None.
HandlerFn = Callable[[str, dict], None]


class _EventBus:
    _singleton: "_EventBus | None" = None

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._subs: dict[str, list[HandlerFn]] = defaultdict(list)

    @classmethod
    def instance(cls) -> "_EventBus":
        if cls._singleton is None:
            cls._singleton = _EventBus()
        return cls._singleton

    def subscribe(self, event_name: str, handler: HandlerFn) -> None:
        with self._lock:
            self._subs[event_name].append(handler)

    def unsubscribe(self, event_name: str, handler: HandlerFn) -> None:
        with self._lock:
            try:
                self._subs[event_name].remove(handler)
            except ValueError:
                pass

    def emit(self, event_name: str, payload: dict) -> int:
        with self._lock:
            handlers = list(self._subs.get(event_name, []))
            wildcard = list(self._subs.get("*", []))
        for h in handlers + wildcard:
            try:
                h(event_name, payload)
            except Exception as e:
                logger.warning(
                    "route_event_handler_failed",
                    event=event_name, error=str(e),
                )
        return len(handlers) + len(wildcard)


def emit(event_name: str, payload: dict | None = None) -> int:
    return _EventBus.instance().emit(event_name, payload or {})


def subscribe(event_name: str, handler: HandlerFn) -> None:
    _EventBus.instance().subscribe(event_name, handler)


def unsubscribe(event_name: str, handler: HandlerFn) -> None:
    _EventBus.instance().unsubscribe(event_name, handler)
