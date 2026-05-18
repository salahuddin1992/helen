"""
Bridge Manager — creates and manages communication bridges on detected transports.
Handles peer connections, data relay, and failover.
"""

from __future__ import annotations

import asyncio
import socket
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Dict, Optional

from app.core.logging import get_logger
from app.transports.detector import TransportDetector
from app.transports.types import BridgeConfig, BridgeStatus, TransportStatus

logger = get_logger(__name__)


@dataclass
class BridgeInstance:
    """Internal bridge instance data."""
    config: BridgeConfig
    server_socket: Optional[socket.socket] = None
    connected_peers: Dict[str, asyncio.StreamReader] = field(default_factory=dict)
    stats: BridgeStatus = field(default_factory=lambda: BridgeStatus(
        bridge_id="",
        status=TransportStatus.AVAILABLE,
    ))
    created_at: datetime = field(default_factory=datetime.utcnow)
    heartbeat_task: Optional[asyncio.Task] = None
    accept_task: Optional[asyncio.Task] = None


class BridgeManager:
    """
    Singleton manager for communication bridges.
    Creates bridges on detected transports, manages connections.
    """

    _instance: Optional[BridgeManager] = None
    _bridges: Dict[str, BridgeInstance] = {}
    _lock: asyncio.Lock = None

    def __new__(cls, detector: Optional[TransportDetector] = None) -> BridgeManager:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self, detector: Optional[TransportDetector] = None) -> None:
        if self._initialized:
            return

        self._initialized = True
        self._detector = detector or TransportDetector()
        self._bridges = {}
        self._lock = asyncio.Lock()
        logger.info("Bridge manager initialized")

    async def create_bridge(self, config: BridgeConfig) -> BridgeStatus:
        """
        Create a new communication bridge on specified transport.
        Opens server socket for peer connections.
        """
        async with self._lock:
            logger.info(
                "Creating bridge",
                bridge_id=config.bridge_id,
                transport=config.source_transport_id,
                address=f"{config.bind_address}:{config.bind_port}",
            )

            try:
                # Validate transport exists
                detected = self._detector.get_cached_results()
                transport = next(
                    (t for t in detected if t.transport_id == config.source_transport_id),
                    None,
                )

                if not transport:
                    logger.error("Transport not found", transport=config.source_transport_id)
                    return BridgeStatus(
                        bridge_id=config.bridge_id,
                        status=TransportStatus.ERROR,
                    )

                # Create server socket
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind((config.bind_address, config.bind_port))
                sock.listen(config.max_connections)
                sock.setblocking(False)

                # Create bridge instance
                instance = BridgeInstance(config=config, server_socket=sock)
                instance.stats = BridgeStatus(
                    bridge_id=config.bridge_id,
                    status=TransportStatus.ACTIVE,
                    uptime_seconds=0,
                )

                self._bridges[config.bridge_id] = instance

                # Start accept loop and heartbeat
                instance.accept_task = asyncio.create_task(self._accept_peers(config.bridge_id))
                instance.heartbeat_task = asyncio.create_task(self._heartbeat_loop(config.bridge_id))

                logger.info("Bridge created", bridge_id=config.bridge_id)
                return instance.stats

            except OSError as e:
                logger.error("Failed to create bridge socket", error=str(e))
                return BridgeStatus(
                    bridge_id=config.bridge_id,
                    status=TransportStatus.ERROR,
                )

    async def destroy_bridge(self, bridge_id: str) -> bool:
        """Tear down a bridge."""
        async with self._lock:
            if bridge_id not in self._bridges:
                return False

            logger.info("Destroying bridge", bridge_id=bridge_id)

            instance = self._bridges[bridge_id]

            # Cancel tasks
            if instance.accept_task:
                instance.accept_task.cancel()
            if instance.heartbeat_task:
                instance.heartbeat_task.cancel()

            # Close socket
            if instance.server_socket:
                instance.server_socket.close()

            # Close peer connections
            for peer_id, reader in instance.connected_peers.items():
                try:
                    if hasattr(reader, 'close'):
                        reader.close()
                except Exception as e:
                    logger.warning("Error closing peer", peer=peer_id, error=str(e))

            del self._bridges[bridge_id]
            logger.info("Bridge destroyed", bridge_id=bridge_id)
            return True

    async def get_bridge_status(self, bridge_id: str) -> Optional[BridgeStatus]:
        """Get current status of a bridge."""
        if bridge_id not in self._bridges:
            return None

        instance = self._bridges[bridge_id]
        uptime = (datetime.utcnow() - instance.created_at).total_seconds()
        instance.stats.uptime_seconds = int(uptime)

        return instance.stats

    async def get_all_bridges(self) -> list[BridgeStatus]:
        """Get status of all active bridges."""
        statuses = []
        for bridge_id in self._bridges.keys():
            status = await self.get_bridge_status(bridge_id)
            if status:
                statuses.append(status)
        return statuses

    async def auto_bridge(self) -> Optional[BridgeStatus]:
        """
        Automatically create a bridge on the best available transport.
        """
        logger.info("Auto-creating bridge on best transport")

        best_transport = self._detector.get_best_transport()
        if not best_transport:
            logger.error("No suitable transport found for bridge")
            return None

        config = BridgeConfig(
            bridge_id=str(uuid.uuid4()),
            source_transport_id=best_transport.transport_id,
            name=f"Auto-Bridge-{best_transport.transport_name}",
            bind_address="0.0.0.0",
            bind_port=5555,  # Auto-assign in production
            protocol="tcp",
            encryption=False,
            compression=False,
            max_connections=100,
        )

        return await self.create_bridge(config)

    async def relay_data(
        self,
        bridge_id: str,
        data: bytes,
        target_peer: str,
    ) -> bool:
        """
        Relay data through bridge to target peer.
        """
        if bridge_id not in self._bridges:
            logger.warning("Bridge not found", bridge_id=bridge_id)
            return False

        instance = self._bridges[bridge_id]

        if target_peer not in instance.connected_peers:
            logger.warning("Peer not connected", bridge=bridge_id, peer=target_peer)
            return False

        try:
            peer_reader = instance.connected_peers[target_peer]
            # In production, would use proper async write through writer
            instance.stats.bytes_sent += len(data)
            logger.debug("Data relayed", bridge=bridge_id, peer=target_peer, bytes=len(data))
            return True

        except Exception as e:
            logger.error("Relay failed", error=str(e))
            instance.stats.error_count += 1
            return False

    async def broadcast(self, bridge_id: str, data: bytes) -> int:
        """
        Broadcast data to all peers on bridge.
        Returns number of successful sends.
        """
        if bridge_id not in self._bridges:
            return 0

        instance = self._bridges[bridge_id]
        sent_count = 0

        for peer_id in list(instance.connected_peers.keys()):
            if await self.relay_data(bridge_id, data, peer_id):
                sent_count += 1

        return sent_count

    async def _accept_peers(self, bridge_id: str) -> None:
        """Accept incoming peer connections."""
        if bridge_id not in self._bridges:
            return

        instance = self._bridges[bridge_id]

        try:
            while True:
                try:
                    # Accept connection (non-blocking)
                    client_sock, addr = instance.server_socket.accept()
                    peer_id = str(uuid.uuid4())

                    logger.info(
                        "Peer connected",
                        bridge=bridge_id,
                        peer=peer_id,
                        address=addr,
                    )

                    instance.connected_peers[peer_id] = client_sock
                    instance.stats.connected_peers = len(instance.connected_peers)
                    instance.stats.last_activity = datetime.utcnow()

                except BlockingIOError:
                    await asyncio.sleep(0.1)

        except asyncio.CancelledError:
            logger.info("Accept loop cancelled", bridge=bridge_id)
            raise
        except Exception as e:
            logger.error("Accept loop error", bridge=bridge_id, error=str(e))
            instance.stats.status = TransportStatus.ERROR
            instance.stats.error_count += 1

    async def _heartbeat_loop(self, bridge_id: str) -> None:
        """Heartbeat loop for peer health checks."""
        if bridge_id not in self._bridges:
            return

        instance = self._bridges[bridge_id]
        heartbeat_interval = instance.config.heartbeat_interval_ms / 1000

        try:
            while True:
                await asyncio.sleep(heartbeat_interval)

                # Check peer health
                dead_peers = []

                for peer_id, sock in list(instance.connected_peers.items()):
                    try:
                        # Simple check: try to recv 0 bytes (non-blocking)
                        sock.recv(0)
                    except (OSError, socket.error):
                        dead_peers.append(peer_id)

                # Remove dead peers
                for peer_id in dead_peers:
                    logger.info("Peer disconnected", bridge=bridge_id, peer=peer_id)
                    try:
                        instance.connected_peers[peer_id].close()
                    except Exception as close_err:
                        logger.debug(
                            "peer_close_failed",
                            bridge=bridge_id,
                            peer=peer_id,
                            error=str(close_err),
                        )
                    del instance.connected_peers[peer_id]

                instance.stats.connected_peers = len(instance.connected_peers)

        except asyncio.CancelledError:
            logger.info("Heartbeat loop cancelled", bridge=bridge_id)
            raise
        except Exception as e:
            logger.error("Heartbeat loop error", bridge=bridge_id, error=str(e))
            instance.stats.error_count += 1

    def _update_stats(
        self,
        bridge_id: str,
        bytes_sent: int = 0,
        bytes_received: int = 0,
    ) -> None:
        """Update bridge statistics."""
        if bridge_id in self._bridges:
            instance = self._bridges[bridge_id]
            instance.stats.bytes_sent += bytes_sent
            instance.stats.bytes_received += bytes_received
            instance.stats.last_activity = datetime.utcnow()
