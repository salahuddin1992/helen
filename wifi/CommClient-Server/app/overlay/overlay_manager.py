"""OverlayManager — top-level orchestrator + lifecycle.

Composes the registry, session manager, and persistence loop into
one entry point. Other packages talk to *this* module; the rest of
the overlay package is private surface.

Loop responsibilities:

  * Periodically evict expired sessions.
  * Optionally persist overlay graphs to ``data/overlay_state.json``.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Optional

from app.core.logging import get_logger
from app.overlay.overlay_config import get_config
from app.overlay.overlay_events import emit, history
from app.overlay.overlay_link import OverlayLink
from app.overlay.overlay_node import OverlayNode
from app.overlay.overlay_registry import get_overlay_registry
from app.overlay.overlay_route import (
    OverlayRoute, resolve_k_shortest, resolve_shortest,
)
from app.overlay.overlay_session import get_overlay_session_manager

logger = get_logger(__name__)


_DATA_DIR = Path(os.environ.get("COMMCLIENT_DATA_DIR",
                 str(Path(__file__).resolve().parents[2] / "data")))
_STATE_FILE = _DATA_DIR / "overlay_state.json"


class OverlayManager:
    _singleton: "OverlayManager | None" = None

    def __init__(self) -> None:
        self._loop_task: Optional[asyncio.Task] = None
        self._running = False
        self._restored = False

    @classmethod
    def instance(cls) -> "OverlayManager":
        if cls._singleton is None:
            cls._singleton = OverlayManager()
        return cls._singleton

    # ── Public API ─────────────────────────────────────────

    def create_overlay(self, name: str):
        return get_overlay_registry().create(name)

    def drop_overlay(self, name: str) -> bool:
        return get_overlay_registry().drop(name)

    def add_node(self, overlay_name: str, node_id: str,
                 *, peer_id: str = "", tags: set[str] | None = None,
                 metadata: dict | None = None) -> OverlayNode:
        graph = get_overlay_registry().require(overlay_name)
        node = OverlayNode(
            overlay_name=overlay_name, node_id=node_id,
            peer_id=peer_id,
            tags=set(tags or []),
            metadata=dict(metadata or {}),
        )
        return graph.add_node(node)

    def add_link(self, overlay_name: str, src_id: str, dst_id: str,
                 *, weight: float = 1.0,
                 bidirectional: bool = False) -> OverlayLink:
        graph = get_overlay_registry().require(overlay_name)
        link = OverlayLink(
            overlay_name=overlay_name, src_id=src_id, dst_id=dst_id,
            weight=weight, bidirectional_hint=bidirectional,
        )
        graph.add_link(link)
        if bidirectional:
            graph.add_link(OverlayLink(
                overlay_name=overlay_name, src_id=dst_id, dst_id=src_id,
                weight=weight, bidirectional_hint=True,
            ))
        return link

    def remove_node(self, overlay_name: str, node_id: str) -> bool:
        """Remove a node + every link touching it from the overlay."""
        graph = get_overlay_registry().require(overlay_name)
        ok = graph.remove_node(node_id)
        if ok:
            emit("overlay.node_removed", {
                "overlay_name": overlay_name, "node_id": node_id,
            })
        return ok

    def remove_link(self, overlay_name: str, src_id: str, dst_id: str,
                    *, bidirectional: bool = False) -> bool:
        """Remove a link (or both directions if bidirectional)."""
        graph = get_overlay_registry().require(overlay_name)
        ok = graph.remove_link(src_id, dst_id)
        if bidirectional:
            ok = graph.remove_link(dst_id, src_id) or ok
        if ok:
            emit("overlay.link_removed", {
                "overlay_name": overlay_name,
                "src_id": src_id, "dst_id": dst_id,
                "bidirectional": bidirectional,
            })
        return ok

    def route(self, overlay_name: str, src_id: str,
              dst_id: str) -> OverlayRoute:
        graph = get_overlay_registry().require(overlay_name)
        r = resolve_shortest(graph, src_id, dst_id)
        emit("overlay.routed", {
            "overlay_name": overlay_name,
            "src_id": src_id, "dst_id": dst_id,
            "hops": r.hop_count,
        })
        return r

    def routes_k(self, overlay_name: str, src_id: str,
                 dst_id: str, *, k: int = 4) -> list[OverlayRoute]:
        graph = get_overlay_registry().require(overlay_name)
        return resolve_k_shortest(graph, src_id, dst_id, k=k)

    # ── Persistence ───────────────────────────────────────

    def persist(self) -> bool:
        cfg = get_config()
        if not cfg.enable_persistence:
            return False
        try:
            _DATA_DIR.mkdir(parents=True, exist_ok=True)
            payload = {
                name: get_overlay_registry().require(name).to_dict()
                for name in get_overlay_registry().list_names()
            }
            tmp = _STATE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, sort_keys=True, indent=2),
                           encoding="utf-8")
            tmp.replace(_STATE_FILE)
            return True
        except Exception as e:
            logger.warning("overlay_persist_failed", error=str(e))
            return False

    def restore(self) -> int:
        if self._restored:
            return 0
        self._restored = True
        if not _STATE_FILE.is_file():
            return 0
        try:
            data = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("overlay_restore_failed", error=str(e))
            return 0
        registry = get_overlay_registry()
        n = 0
        for overlay_name, dump in (data or {}).items():
            graph = registry.create(overlay_name)
            for nd in (dump.get("nodes") or []):
                graph.add_node(OverlayNode.from_dict(nd))
            for lk in (dump.get("links") or []):
                graph.add_link(OverlayLink.from_dict(lk))
            n += 1
        return n

    # ── Background loop ───────────────────────────────────

    async def _run_loop(self) -> None:
        cfg = get_config()
        self._running = True
        logger.info("overlay_manager_started",
                    interval_sec=cfg.refresh_interval_sec)
        try:
            while self._running:
                try:
                    get_overlay_session_manager().evict_expired()
                    self.persist()
                except Exception as e:
                    logger.warning("overlay_cycle_failed", error=str(e))
                await asyncio.sleep(cfg.refresh_interval_sec)
        finally:
            logger.info("overlay_manager_stopped")

    def start(self) -> None:
        if self._loop_task is not None and not self._loop_task.done():
            return
        self.restore()
        try:
            loop = asyncio.get_event_loop()
            self._loop_task = loop.create_task(
                self._run_loop(), name="overlay-manager",
            )
        except RuntimeError:
            logger.warning("overlay_manager_no_event_loop_yet")

    def stop(self) -> None:
        self._running = False
        self.persist()
        if self._loop_task is not None:
            self._loop_task.cancel()
            self._loop_task = None

    # ── Diagnostics ───────────────────────────────────────

    def snapshot(self) -> dict:
        return {
            "started":  self._running,
            "registry": get_overlay_registry().snapshot(),
            "sessions": get_overlay_session_manager().snapshot(),
            "events":   history(limit=50),
        }


def get_overlay_manager() -> OverlayManager:
    return OverlayManager.instance()


def start_overlay() -> None:
    get_overlay_manager().start()


def stop_overlay() -> None:
    get_overlay_manager().stop()
