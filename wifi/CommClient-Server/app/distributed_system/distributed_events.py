"""Distributed-system event bus — pub/sub across managers.

Managers emit events (``node.joined``, ``leader.acquired``,
``partition.detected``, ``recovery.started``) and any number of
listeners subscribe. Synchronous in-process; for cross-process
fanout, the audit_replication module already provides hash-chained
durability.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from typing import Callable

from app.core.logging import get_logger

logger = get_logger(__name__)


HandlerFn = Callable[[str, dict], None]


class _EventBus:
    _singleton: "_EventBus | None" = None

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._subs: dict[str, list[HandlerFn]] = defaultdict(list)
        self._history: list[tuple[float, str, dict]] = []
        self._history_max = 200

    @classmethod
    def instance(cls) -> "_EventBus":
        if cls._singleton is None:
            cls._singleton = _EventBus()
        return cls._singleton

    def subscribe(self, name: str, handler: HandlerFn) -> None:
        with self._lock:
            self._subs[name].append(handler)

    def unsubscribe(self, name: str, handler: HandlerFn) -> None:
        with self._lock:
            try:
                self._subs[name].remove(handler)
            except ValueError:
                pass

    def emit(self, name: str, payload: dict | None = None) -> int:
        import time
        payload = payload or {}
        with self._lock:
            handlers = list(self._subs.get(name, []))
            wildcard = list(self._subs.get("*", []))
            self._history.append((time.time(), name, payload))
            self._history = self._history[-self._history_max:]
        for h in handlers + wildcard:
            try:
                h(name, payload)
            except Exception as e:
                logger.warning(
                    "ds_event_handler_failed",
                    event=name, error=str(e),
                )
        return len(handlers) + len(wildcard)

    def history(self, limit: int = 50) -> list[dict]:
        with self._lock:
            return [
                {"ts": t, "event": n, "payload": p}
                for t, n, p in self._history[-int(limit):]
            ]


def emit(event: str, payload: dict | None = None) -> int:
    return _EventBus.instance().emit(event, payload)


def subscribe(event: str, handler: HandlerFn) -> None:
    _EventBus.instance().subscribe(event, handler)


def unsubscribe(event: str, handler: HandlerFn) -> None:
    _EventBus.instance().unsubscribe(event, handler)


def history(limit: int = 50) -> list[dict]:
    return _EventBus.instance().history(limit)
