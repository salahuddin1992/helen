"""Overlay event bus — independent of the other packages' buses."""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from typing import Callable

from app.core.logging import get_logger

logger = get_logger(__name__)

HandlerFn = Callable[[str, dict], None]


class _Bus:
    _singleton: "_Bus | None" = None

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._subs: dict[str, list[HandlerFn]] = defaultdict(list)
        self._history: list[tuple[float, str, dict]] = []
        self._cap = 200

    @classmethod
    def instance(cls) -> "_Bus":
        if cls._singleton is None:
            cls._singleton = _Bus()
        return cls._singleton

    def subscribe(self, name: str, h: HandlerFn) -> None:
        with self._lock:
            self._subs[name].append(h)

    def emit(self, name: str, payload: dict | None = None) -> int:
        payload = payload or {}
        with self._lock:
            handlers = list(self._subs.get(name, []))
            wildcard = list(self._subs.get("*", []))
            self._history.append((time.time(), name, payload))
            self._history = self._history[-self._cap:]
        for h in handlers + wildcard:
            try:
                h(name, payload)
            except Exception as e:
                logger.warning("overlay_event_failed",
                               event=name, error=str(e))
        return len(handlers) + len(wildcard)

    def history(self, limit: int = 50) -> list[dict]:
        with self._lock:
            return [
                {"ts": t, "event": n, "payload": p}
                for t, n, p in self._history[-int(limit):]
            ]


def emit(name: str, payload: dict | None = None) -> int:
    return _Bus.instance().emit(name, payload)


def subscribe(name: str, handler: HandlerFn) -> None:
    _Bus.instance().subscribe(name, handler)


def history(limit: int = 50) -> list[dict]:
    return _Bus.instance().history(limit)
