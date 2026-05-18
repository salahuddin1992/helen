"""Peer relay — when this node acts as a relay for others.

The actual relay endpoint lives in api/routes/cluster.py
(``/api/cluster/relay``); this file exposes the *p2p-flavoured*
view: count of relays performed, average chain length, recent
forwards.
"""

from __future__ import annotations

import threading
import time
from collections import deque


class RelayStats:
    _singleton: "RelayStats | None" = None

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._count = 0
        self._success = 0
        self._failed = 0
        self._chain_lengths = deque(maxlen=200)
        self._last_at: float = 0.0

    @classmethod
    def instance(cls) -> "RelayStats":
        if cls._singleton is None:
            cls._singleton = RelayStats()
        return cls._singleton

    def record(self, success: bool, chain_length: int) -> None:
        with self._lock:
            self._count += 1
            if success:
                self._success += 1
            else:
                self._failed += 1
            self._chain_lengths.append(int(chain_length))
            self._last_at = time.time()

    def snapshot(self) -> dict:
        with self._lock:
            avg = (sum(self._chain_lengths) / max(1, len(self._chain_lengths))
                   if self._chain_lengths else 0)
            return {
                "count":              self._count,
                "success":            self._success,
                "failed":             self._failed,
                "avg_chain_length":   round(avg, 2),
                "last_at":            self._last_at,
            }


def get_relay_stats() -> RelayStats:
    return RelayStats.instance()
