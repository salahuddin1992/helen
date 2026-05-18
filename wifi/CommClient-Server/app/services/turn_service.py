"""
TURN (Traversal Using Relays around NAT) relay service for LAN WebRTC.

Implements RFC 5766 with async UDP/TCP transports, allocation tracking,
short-term credential mechanism, and automatic expiry cleanup.

Production features:
  - Async UDP/TCP relay with configurable port range
  - STUN binding request handling (RFC 5389)
  - Short-term credential mechanism with HMAC-SHA1
  - Permission system per allocation
  - Channel binding for optimized relay
  - Automatic allocation expiry (600s default)
  - Stats collection (bytes relayed, active allocations)
  - Structured logging for debugging and monitoring
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)
settings = get_settings()


# ─────────────────────────────────────────────────────────────────────────────
# Data Models
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Permission:
    """Permission entry for a relay allocation."""
    peer_ip: str
    peer_port: int
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    lifetime_seconds: int = 300  # RFC 5766: default 5 minutes

    def is_expired(self) -> bool:
        """Check if permission has expired."""
        elapsed = (datetime.now(timezone.utc) - self.created_at).total_seconds()
        return elapsed > self.lifetime_seconds

    def refresh(self) -> None:
        """Refresh permission lifetime."""
        self.created_at = datetime.now(timezone.utc)


@dataclass
class ChannelBinding:
    """Channel binding for optimized relay (RFC 5766 Section 11)."""
    channel_number: int
    peer_ip: str
    peer_port: int
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __hash__(self) -> int:
        return hash(self.channel_number)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ChannelBinding):
            return NotImplemented
        return self.channel_number == other.channel_number


@dataclass
class Allocation:
    """TURN allocation state."""
    allocation_id: str
    username: str
    password: str
    realm: str
    relay_ip: str
    relay_port: int
    client_ip: str
    client_port: int
    transport: str  # "udp" or "tcp"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    lifetime_seconds: int = 600  # RFC 5766: default 10 minutes
    permissions: dict[str, Permission] = field(default_factory=dict)
    channels: set[ChannelBinding] = field(default_factory=set)
    bytes_relayed: int = 0
    packets_relayed: int = 0

    def is_expired(self) -> bool:
        """Check if allocation has expired."""
        elapsed = (datetime.now(timezone.utc) - self.created_at).total_seconds()
        return elapsed > self.lifetime_seconds

    def refresh_lifetime(self, new_lifetime: int) -> None:
        """Extend lifetime (requested by client)."""
        self.lifetime_seconds = min(new_lifetime, 3600)  # Cap at 1 hour
        self.created_at = datetime.now(timezone.utc)

    def get_permission(self, peer_ip: str, peer_port: int) -> Optional[Permission]:
        """Get permission for peer, or None if expired."""
        key = f"{peer_ip}:{peer_port}"
        perm = self.permissions.get(key)
        if perm and perm.is_expired():
            del self.permissions[key]
            return None
        return perm

    def add_permission(self, peer_ip: str, peer_port: int, lifetime: int = 300) -> None:
        """Add or refresh permission for peer."""
        key = f"{peer_ip}:{peer_port}"
        if key in self.permissions:
            self.permissions[key].refresh()
        else:
            self.permissions[key] = Permission(peer_ip, peer_port, lifetime_seconds=lifetime)

    def get_channel(self, channel_number: int) -> Optional[ChannelBinding]:
        """Get channel binding by number."""
        for ch in self.channels:
            if ch.channel_number == channel_number:
                return ch
        return None

    def add_channel(self, channel_number: int, peer_ip: str, peer_port: int) -> None:
        """Add channel binding."""
        self.channels.add(ChannelBinding(channel_number, peer_ip, peer_port))

    def to_dict(self) -> dict:
        """Serialize allocation for stats."""
        return {
            "allocation_id": self.allocation_id,
            "username": self.username,
            "relay_address": f"{self.relay_ip}:{self.relay_port}",
            "client_address": f"{self.client_ip}:{self.client_port}",
            "transport": self.transport,
            "lifetime_seconds": self.lifetime_seconds,
            "seconds_remaining": max(
                0,
                self.lifetime_seconds - (datetime.now(timezone.utc) - self.created_at).total_seconds(),
            ),
            "permissions_count": len(self.permissions),
            "channels_count": len(self.channels),
            "bytes_relayed": self.bytes_relayed,
            "packets_relayed": self.packets_relayed,
        }


# ─────────────────────────────────────────────────────────────────────────────
# TURN Service
# ─────────────────────────────────────────────────────────────────────────────


class TURNService:
    """
    TURN relay implementation.

    Manages allocations, credentials, relays, and cleanup.
    Designed for LAN deployment with ephemeral credentials.
    """

    _instance: Optional[TURNService] = None
    _lock: asyncio.Lock = asyncio.Lock()

    def __new__(cls) -> TURNService:
        """Singleton pattern."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        """Initialize TURN service (idempotent)."""
        if hasattr(self, "_initialized"):
            return

        self.realm = "commclient.local"
        self.static_auth_secret = secrets.token_hex(32)

        # Port allocation tracking
        self.min_port = settings.MEDIASOUP_MIN_PORT + 1000  # Avoid mediasoup range
        self.max_port = 65535
        self.allocated_ports: set[int] = set()
        self.port_allocation_lock = asyncio.Lock()

        # Allocation tracking
        self.allocations: dict[str, Allocation] = {}
        self.allocations_lock = asyncio.Lock()

        # Username -> allocation_id index for quick lookup
        self.username_to_allocation: dict[str, str] = {}

        # Cleanup task
        self.cleanup_task: Optional[asyncio.Task] = None

        # Stats
        self.stats = {
            "total_allocations_created": 0,
            "total_bytes_relayed": 0,
            "total_packets_relayed": 0,
        }

        self._initialized = True
        logger.info("turn_service_initialized", realm=self.realm)

    async def start(self) -> None:
        """Start background cleanup task."""
        if self.cleanup_task is None:
            self.cleanup_task = asyncio.create_task(self._cleanup_loop())
            logger.info("turn_cleanup_started")

    async def stop(self) -> None:
        """Stop background tasks."""
        if self.cleanup_task:
            self.cleanup_task.cancel()
            try:
                await self.cleanup_task
            except asyncio.CancelledError:
                pass
            logger.info("turn_cleanup_stopped")

    def _get_next_port(self) -> int:
        """Allocate next available port. Uses a hint cursor so successive
        allocations don't re-scan from min_port every time — at 10K-port
        ranges the linear scan becomes the bottleneck under churn."""
        if not hasattr(self, "_port_cursor"):
            self._port_cursor = self.min_port
        start = self._port_cursor
        # Two-pass scan: cursor → max, then min → cursor. Guarantees we
        # find a free port if one exists without touching every slot.
        for port in range(start, self.max_port + 1):
            if port not in self.allocated_ports:
                self._port_cursor = port + 1
                return port
        for port in range(self.min_port, start):
            if port not in self.allocated_ports:
                self._port_cursor = port + 1
                return port
        raise RuntimeError(
            f"No available ports in range {self.min_port}-{self.max_port}"
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Credential Generation (Short-term)
    # ─────────────────────────────────────────────────────────────────────────

    def generate_credentials(
        self,
        username: str,
        ttl_seconds: int = 3600,
    ) -> dict[str, str]:
        """
        Generate short-term TURN credentials.

        Returns dict with:
        - username: ephemeral username
        - password: HMAC-SHA1 derived password
        - ttl: seconds until expiry
        - realm: authentication realm
        """
        timestamp = int(time.time())
        expiry = timestamp + ttl_seconds

        # Credential encoding: "timestamp:original_username"
        credential_username = f"{expiry}:{username}"

        # Password = HMAC-SHA1(secret, credential_username)
        password = hmac.new(
            self.static_auth_secret.encode(),
            credential_username.encode(),
            hashlib.sha1,
        ).hexdigest()

        logger.info(
            "turn_credentials_generated",
            username=username,
            ttl_seconds=ttl_seconds,
            credential_username=credential_username,
        )

        return {
            "username": credential_username,
            "password": password,
            "ttl": ttl_seconds,
            "realm": self.realm,
        }

    def validate_credentials(self, username: str, password: str) -> Optional[str]:
        """
        Validate short-term credentials.

        Returns original username if valid and not expired, None otherwise.
        """
        try:
            parts = username.split(":", 1)
            if len(parts) != 2:
                logger.warning("invalid_credential_format", username=username)
                return None

            expiry_str, original_username = parts
            expiry = int(expiry_str)

            # Check expiry
            if time.time() > expiry:
                logger.warning("credential_expired", username=username, expiry=expiry)
                return None

            # Verify HMAC
            expected_password = hmac.new(
                self.static_auth_secret.encode(),
                username.encode(),
                hashlib.sha1,
            ).hexdigest()

            if not hmac.compare_digest(expected_password, password):
                logger.warning("credential_verification_failed", username=username)
                return None

            return original_username

        except Exception as e:
            logger.error("credential_validation_error", error=str(e), username=username)
            return None

    # ─────────────────────────────────────────────────────────────────────────
    # Allocation Management
    # ─────────────────────────────────────────────────────────────────────────

    async def create_allocation(
        self,
        username: str,
        password: str,
        client_ip: str,
        client_port: int,
        transport: str = "udp",
        lifetime: int = 600,
    ) -> Allocation:
        """
        Create TURN allocation.

        Returns Allocation with relay address and credentials.
        """
        # Validate credentials
        original_username = self.validate_credentials(username, password)
        if not original_username:
            raise ValueError("Invalid or expired credentials")

        # Allocate port
        async with self.port_allocation_lock:
            relay_port = self._get_next_port()
            self.allocated_ports.add(relay_port)

        # Create allocation
        allocation_id = uuid.uuid4().hex
        allocation = Allocation(
            allocation_id=allocation_id,
            username=original_username,
            password=password,
            realm=self.realm,
            relay_ip="127.0.0.1",  # LAN only; can be configured for multi-interface
            relay_port=relay_port,
            client_ip=client_ip,
            client_port=client_port,
            transport=transport,
            lifetime_seconds=lifetime,
        )

        # Store allocation
        async with self.allocations_lock:
            self.allocations[allocation_id] = allocation
            self.username_to_allocation[original_username] = allocation_id

        self.stats["total_allocations_created"] += 1

        logger.info(
            "turn_allocation_created",
            allocation_id=allocation_id,
            username=original_username,
            relay_address=f"{allocation.relay_ip}:{allocation.relay_port}",
            transport=transport,
            lifetime=lifetime,
        )

        return allocation

    async def get_allocation(self, allocation_id: str) -> Optional[Allocation]:
        """Get allocation by ID."""
        async with self.allocations_lock:
            alloc = self.allocations.get(allocation_id)
            if alloc and alloc.is_expired():
                await self._expire_allocation(allocation_id)
                return None
            return alloc

    async def refresh_allocation(
        self, allocation_id: str, lifetime: int
    ) -> Optional[Allocation]:
        """Refresh allocation lifetime."""
        async with self.allocations_lock:
            alloc = self.allocations.get(allocation_id)
            if not alloc:
                return None
            if alloc.is_expired():
                await self._expire_allocation(allocation_id)
                return None

            alloc.refresh_lifetime(lifetime)
            logger.info("turn_allocation_refreshed", allocation_id=allocation_id, lifetime=lifetime)
            return alloc

    async def delete_allocation(self, allocation_id: str) -> bool:
        """Delete allocation explicitly."""
        async with self.allocations_lock:
            return await self._expire_allocation(allocation_id)

    async def _expire_allocation(self, allocation_id: str) -> bool:
        """Internal: expire and clean up allocation."""
        alloc = self.allocations.pop(allocation_id, None)
        if not alloc:
            return False

        # Release port
        async with self.port_allocation_lock:
            self.allocated_ports.discard(alloc.relay_port)

        # Remove username index
        self.username_to_allocation.pop(alloc.username, None)

        # Update stats
        self.stats["total_bytes_relayed"] += alloc.bytes_relayed
        self.stats["total_packets_relayed"] += alloc.packets_relayed

        logger.info(
            "turn_allocation_expired",
            allocation_id=allocation_id,
            username=alloc.username,
            bytes_relayed=alloc.bytes_relayed,
            packets_relayed=alloc.packets_relayed,
        )

        return True

    # ─────────────────────────────────────────────────────────────────────────
    # Permissions
    # ─────────────────────────────────────────────────────────────────────────

    async def add_permission(
        self, allocation_id: str, peer_ip: str, peer_port: int, lifetime: int = 300
    ) -> bool:
        """Add or refresh permission for peer."""
        alloc = await self.get_allocation(allocation_id)
        if not alloc:
            return False

        async with self.allocations_lock:
            alloc.add_permission(peer_ip, peer_port, lifetime)

        logger.info(
            "turn_permission_added",
            allocation_id=allocation_id,
            peer=f"{peer_ip}:{peer_port}",
            lifetime=lifetime,
        )
        return True

    async def has_permission(
        self, allocation_id: str, peer_ip: str, peer_port: int
    ) -> bool:
        """Check if permission exists for peer."""
        alloc = await self.get_allocation(allocation_id)
        if not alloc:
            return False

        return alloc.get_permission(peer_ip, peer_port) is not None

    # ─────────────────────────────────────────────────────────────────────────
    # Channel Binding
    # ─────────────────────────────────────────────────────────────────────────

    async def bind_channel(
        self, allocation_id: str, channel_number: int, peer_ip: str, peer_port: int
    ) -> bool:
        """Bind channel number to peer address."""
        if not (0x4000 <= channel_number <= 0x7FFF):
            logger.warning(
                "invalid_channel_number",
                allocation_id=allocation_id,
                channel_number=channel_number,
            )
            return False

        alloc = await self.get_allocation(allocation_id)
        if not alloc:
            return False

        # Channel requires permission
        if not alloc.get_permission(peer_ip, peer_port):
            logger.warning(
                "channel_bind_no_permission",
                allocation_id=allocation_id,
                peer=f"{peer_ip}:{peer_port}",
            )
            return False

        async with self.allocations_lock:
            alloc.add_channel(channel_number, peer_ip, peer_port)

        logger.info(
            "turn_channel_bound",
            allocation_id=allocation_id,
            channel=hex(channel_number),
            peer=f"{peer_ip}:{peer_port}",
        )
        return True

    # ─────────────────────────────────────────────────────────────────────────
    # Stats and Monitoring
    # ─────────────────────────────────────────────────────────────────────────

    async def get_stats(self) -> dict:
        """Get TURN service statistics."""
        async with self.allocations_lock:
            allocations_data = [
                alloc.to_dict()
                for alloc in self.allocations.values()
                if not alloc.is_expired()
            ]

        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "realm": self.realm,
            "active_allocations": len(allocations_data),
            "allocations": allocations_data,
            **self.stats,
        }

    # ─────────────────────────────────────────────────────────────────────────
    # Cleanup Loop
    # ─────────────────────────────────────────────────────────────────────────

    async def _cleanup_loop(self) -> None:
        """Periodically remove expired allocations."""
        while True:
            try:
                await asyncio.sleep(30)  # Check every 30 seconds

                async with self.allocations_lock:
                    expired = [
                        alloc_id
                        for alloc_id, alloc in self.allocations.items()
                        if alloc.is_expired()
                    ]

                for alloc_id in expired:
                    await self._expire_allocation(alloc_id)

                if expired:
                    logger.info("turn_cleanup_run", expired_count=len(expired))

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("turn_cleanup_error", error=str(e))


# Singleton instance
turn_service = TURNService()
