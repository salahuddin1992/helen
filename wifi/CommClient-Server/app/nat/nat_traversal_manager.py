"""NAT traversal manager — orchestrates the strategy ladder.

For each ``traverse(peer_id)`` call the manager runs:

    1. Reuse cached session (if fresh + successful).
    2. Direct (already public on both ends).
    3. UDP hole-punch.
    4. TCP simultaneous open.
    5. Reverse tunnel via Helen-Rendezvous.
    6. Relay fallback through the mesh.

The first strategy that succeeds is recorded in a NATSession so the
next call short-circuits.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from app.core.logging import get_logger
from app.nat.nat_config import get_config
from app.nat.nat_detector import get_nat_detector
from app.nat.nat_events import emit, history
from app.nat.nat_exceptions import (
    NATNotTraversableError, RelayFallbackError, ReverseTunnelError,
)
from app.nat.nat_session import get_session_manager
from app.nat.nat_type import NATType, best_strategy
from app.nat import (
    rendezvous_client, relay_fallback, reverse_tunnel,
    tcp_hole_punch, udp_hole_punch,
)

logger = get_logger(__name__)


class NATTraversalManager:
    _singleton: "NATTraversalManager | None" = None

    def __init__(self) -> None:
        self._loop_task: Optional[asyncio.Task] = None
        self._running = False

    @classmethod
    def instance(cls) -> "NATTraversalManager":
        if cls._singleton is None:
            cls._singleton = NATTraversalManager()
        return cls._singleton

    # ── Strategy ladder ───────────────────────────────────

    async def traverse(self, peer_id: str,
                       *, peer_nat: NATType = NATType.UNKNOWN) -> str:
        """Run the ladder; return the name of the first strategy that
        succeeded ('direct' / 'udp_punch' / 'tcp_punch' /
        'reverse_tunnel' / 'relay'). Raises NATNotTraversableError if
        none worked.
        """
        cache = get_session_manager().get(peer_id)
        if cache and cache.success:
            cache.touch()
            emit("nat.cache_hit", {
                "peer_id":  peer_id,
                "strategy": cache.strategy,
            })
            return cache.strategy

        local_type = get_nat_detector().current()
        suggested = best_strategy(local_type, peer_nat)

        # 1. Direct.
        if suggested == "direct":
            get_session_manager().open(peer_id, "direct", success=True)
            emit("nat.traverse_ok", {"peer_id": peer_id,
                                       "strategy": "direct"})
            return "direct"

        # 2. UDP hole-punch.
        if get_config().enable_udp_punch:
            try:
                ok = await udp_hole_punch.punch(peer_id)
                if ok:
                    get_session_manager().open(
                        peer_id, "udp_punch", success=True,
                    )
                    emit("nat.traverse_ok", {"peer_id": peer_id,
                                               "strategy": "udp_punch"})
                    return "udp_punch"
            except Exception as e:
                logger.debug("nat_udp_punch_failed", error=str(e)[:80])

        # 3. TCP punch.
        if get_config().enable_tcp_punch:
            try:
                sock = await tcp_hole_punch.punch(peer_id)
                if sock is not None:
                    try:
                        sock.close()  # we just wanted to confirm reachability
                    except Exception:
                        pass
                    get_session_manager().open(
                        peer_id, "tcp_punch", success=True,
                    )
                    emit("nat.traverse_ok", {"peer_id": peer_id,
                                               "strategy": "tcp_punch"})
                    return "tcp_punch"
            except Exception as e:
                logger.debug("nat_tcp_punch_failed", error=str(e)[:80])

        # 4. Reverse tunnel (only useful if peer is the one behind NAT
        #    and the tunnel exists; otherwise we just record the
        #    rendezvous as an intermediary).
        if get_config().enable_reverse_tunnel and rendezvous_client.is_configured():
            try:
                up = await reverse_tunnel.start()
                if up:
                    get_session_manager().open(
                        peer_id, "reverse_tunnel", success=True,
                    )
                    emit("nat.traverse_ok", {"peer_id": peer_id,
                                               "strategy": "reverse_tunnel"})
                    return "reverse_tunnel"
            except ReverseTunnelError as e:
                logger.debug("nat_tunnel_failed", error=str(e)[:80])

        # 5. Relay fallback — always available as long as the mesh is up.
        if get_config().enable_relay_fallback:
            get_session_manager().open(peer_id, "relay", success=True,
                                        last_error="fallback_only")
            emit("nat.traverse_ok", {"peer_id": peer_id,
                                       "strategy": "relay"})
            return "relay"

        get_session_manager().open(peer_id, "none", success=False,
                                    last_error="all_strategies_failed")
        emit("nat.traverse_failed", {"peer_id": peer_id})
        raise NATNotTraversableError(peer_id)

    # ── Lifecycle ────────────────────────────────────────

    async def _bootstrap(self) -> None:
        """Initial NAT detection at startup."""
        try:
            await get_nat_detector().detect_once()
        except Exception as e:
            logger.warning("nat_initial_detect_failed", error=str(e))
        # Also kick the long-running detector loop.
        get_nat_detector().start()

    async def _maintenance_loop(self) -> None:
        self._running = True
        logger.info("nat_manager_started")
        try:
            while self._running:
                try:
                    get_session_manager().evict_expired()
                except Exception as e:
                    logger.warning("nat_maintenance_failed", error=str(e))
                await asyncio.sleep(30.0)
        finally:
            logger.info("nat_manager_stopped")

    def start(self) -> None:
        if self._loop_task is not None and not self._loop_task.done():
            return
        try:
            loop = asyncio.get_event_loop()
            loop.create_task(self._bootstrap())
            self._loop_task = loop.create_task(
                self._maintenance_loop(), name="nat-manager",
            )
        except RuntimeError:
            logger.warning("nat_manager_no_event_loop_yet")

    def stop(self) -> None:
        self._running = False
        get_nat_detector().stop()
        if self._loop_task is not None:
            self._loop_task.cancel()
            self._loop_task = None

    # ── Diagnostics ──────────────────────────────────────

    def snapshot(self) -> dict:
        return {
            "started":     self._running,
            "detector":    get_nat_detector().snapshot(),
            "rendezvous":  rendezvous_client.snapshot(),
            "udp_punch":   udp_hole_punch.snapshot(),
            "tcp_punch":   tcp_hole_punch.snapshot(),
            "tunnel":      reverse_tunnel.snapshot(),
            "relay":       relay_fallback.snapshot(),
            "sessions":    get_session_manager().snapshot(),
            "events":      history(limit=50),
        }


def get_nat_manager() -> NATTraversalManager:
    return NATTraversalManager.instance()


def start_nat() -> None:
    get_nat_manager().start()


def stop_nat() -> None:
    get_nat_manager().stop()
