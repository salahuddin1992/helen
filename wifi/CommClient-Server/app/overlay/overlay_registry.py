"""OverlayRegistry — singleton index of named overlays.

Each entry maps ``overlay_name → OverlayGraph``. Operators create
new overlays by name; deletion drops the graph + its sessions.
"""

from __future__ import annotations

import threading
from typing import Optional

from app.overlay.overlay_config import get_config
from app.overlay.overlay_events import emit
from app.overlay.overlay_exceptions import (
    OverlayConfigError, OverlayNotFoundError,
)
from app.overlay.overlay_graph import OverlayGraph


class OverlayRegistry:
    _singleton: "OverlayRegistry | None" = None

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._overlays: dict[str, OverlayGraph] = {}

    @classmethod
    def instance(cls) -> "OverlayRegistry":
        if cls._singleton is None:
            cls._singleton = OverlayRegistry()
        return cls._singleton

    # ── CRUD ─────────────────────────────────────────────

    def create(self, overlay_name: str) -> OverlayGraph:
        cfg = get_config()
        name = (overlay_name or "").strip()
        if not name:
            raise OverlayConfigError("overlay_name required")
        with self._lock:
            if name in self._overlays:
                return self._overlays[name]
            if len(self._overlays) >= cfg.max_overlays:
                raise OverlayConfigError(
                    f"max_overlays ({cfg.max_overlays}) reached"
                )
            g = OverlayGraph(name)
            self._overlays[name] = g
        emit("overlay.created", {"overlay_name": name})
        return g

    def drop(self, overlay_name: str) -> bool:
        with self._lock:
            removed = self._overlays.pop(overlay_name, None) is not None
        if removed:
            emit("overlay.dropped", {"overlay_name": overlay_name})
        return removed

    def get(self, overlay_name: str) -> Optional[OverlayGraph]:
        with self._lock:
            return self._overlays.get(overlay_name)

    def require(self, overlay_name: str) -> OverlayGraph:
        g = self.get(overlay_name)
        if g is None:
            raise OverlayNotFoundError(overlay_name)
        return g

    def list_names(self) -> list[str]:
        with self._lock:
            return sorted(self._overlays.keys())

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "count":    len(self._overlays),
                "names":    sorted(self._overlays.keys()),
                "overlays": {
                    name: g.stats()
                    for name, g in self._overlays.items()
                },
            }


def get_overlay_registry() -> OverlayRegistry:
    return OverlayRegistry.instance()
