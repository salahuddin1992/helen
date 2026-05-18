"""P2P manager — top-level lifecycle for the package.

Starts a single background loop that:

  1. Seeds bootstrap peers (HELEN_BOOTSTRAP_PEERS env).
  2. Pulls services.peer_registry → p2p.peer_registry every cycle.
  3. Triggers a gossip cycle.
  4. Snapshots state for the admin endpoint.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from app.core.logging import get_logger

from app.p2p.p2p_config import get_config
from app.p2p.peer_bootstrap import seed_bootstrap_peers
from app.p2p.peer_bridge import bridge_snapshot
from app.p2p.peer_dht import dht_snapshot
from app.p2p.peer_discovery import discovery_snapshot, sync_from_services
from app.p2p.peer_events import emit, history
from app.p2p.peer_federation import federation_snapshot
from app.p2p.peer_gossip import gossip_snapshot, trigger_gossip_cycle
from app.p2p.peer_identity import identity_snapshot
from app.p2p.peer_message_bus import get_message_bus
from app.p2p.peer_nat_traversal import nat_snapshot
from app.p2p.peer_registry import get_p2p_registry
from app.p2p.peer_relay import get_relay_stats
from app.p2p.peer_selection import selection_snapshot
from app.p2p.peer_session import get_session_manager

logger = get_logger(__name__)


class P2PManager:
    _singleton: "P2PManager | None" = None

    def __init__(self) -> None:
        self._loop_task: Optional[asyncio.Task] = None
        self._running = False
        self._cycles = 0

    @classmethod
    def instance(cls) -> "P2PManager":
        if cls._singleton is None:
            cls._singleton = P2PManager()
        return cls._singleton

    # ── Background loop ─────────────────────────────────────

    async def _run_loop(self) -> None:
        cfg = get_config()
        self._running = True
        logger.info("p2p_manager_started",
                    interval_sec=cfg.refresh_interval_sec)
        # Seed bootstrap on first run.
        try:
            seeded = await seed_bootstrap_peers()
            if seeded:
                logger.info("p2p_bootstrap_seeded", count=seeded)
        except Exception as e:
            logger.warning("p2p_bootstrap_failed", error=str(e))

        try:
            while self._running:
                try:
                    n = await sync_from_services()
                    if n:
                        emit("p2p.synced_from_services", {"count": n})
                except Exception as e:
                    logger.debug("p2p_sync_failed", error=str(e))
                try:
                    await trigger_gossip_cycle()
                except Exception:
                    pass
                self._cycles += 1
                await asyncio.sleep(cfg.refresh_interval_sec)
        finally:
            logger.info("p2p_manager_stopped")

    def start(self) -> None:
        if self._loop_task is not None and not self._loop_task.done():
            return
        try:
            loop = asyncio.get_event_loop()
            self._loop_task = loop.create_task(
                self._run_loop(), name="p2p-manager",
            )
        except RuntimeError:
            logger.warning("p2p_manager_no_event_loop_yet")

    def stop(self) -> None:
        self._running = False
        if self._loop_task is not None:
            self._loop_task.cancel()
            self._loop_task = None

    # ── Diagnostics ─────────────────────────────────────────

    def snapshot(self) -> dict:
        return {
            "started":    self._running,
            "cycles":     self._cycles,
            "identity":   identity_snapshot(),
            "registry":   get_p2p_registry().snapshot(),
            "selection":  selection_snapshot(),
            "discovery":  discovery_snapshot(),
            "gossip":     gossip_snapshot(),
            "dht":        dht_snapshot(),
            "bridge":     bridge_snapshot(),
            "federation": federation_snapshot(),
            "nat":        nat_snapshot(),
            "sessions":   get_session_manager().snapshot(),
            "relay":      get_relay_stats().snapshot(),
            "message_bus": get_message_bus().stats(),
            "events":     history(limit=50),
        }


def get_p2p_manager() -> P2PManager:
    return P2PManager.instance()


def start_p2p() -> None:
    get_p2p_manager().start()


def stop_p2p() -> None:
    get_p2p_manager().stop()
