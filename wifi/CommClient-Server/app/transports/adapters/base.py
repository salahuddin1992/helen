"""
Abstract base adapter class for all transport families.
Defines interface that all transport adapters must implement.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)


class BaseTransportAdapter(ABC):
    """
    Abstract base class for transport adapters.

    Each adapter family (ethernet, wifi, fiber, etc.) subclasses this
    to provide family-specific detection and communication logic.
    """

    family: str = ""
    display_name: str = ""

    @abstractmethod
    async def detect(self) -> list[dict[str, Any]]:
        """
        Detect available transports of this family.

        Returns:
            List of detected transport dictionaries with interface details.
            Each dict contains at minimum: interface, status, metadata.
        """
        ...

    @abstractmethod
    async def connect(self, interface: str, config: dict[str, Any]) -> Any:
        """
        Establish connection on this transport.

        Args:
            interface: Interface name/identifier to connect on
            config: Connection configuration dictionary

        Returns:
            Connection object/handle for subsequent I/O operations
        """
        ...

    @abstractmethod
    async def disconnect(self, connection_id: str) -> bool:
        """
        Disconnect from transport.

        Args:
            connection_id: Identifier of active connection to close

        Returns:
            True if disconnect successful, False otherwise
        """
        ...

    @abstractmethod
    async def send(self, connection_id: str, data: bytes) -> int:
        """
        Send data over transport.

        Args:
            connection_id: Identifier of active connection
            data: Bytes to transmit

        Returns:
            Number of bytes successfully sent
        """
        ...

    @abstractmethod
    async def receive(
        self, connection_id: str, buffer_size: int = 65536
    ) -> bytes:
        """
        Receive data from transport.

        Args:
            connection_id: Identifier of active connection
            buffer_size: Maximum bytes to receive

        Returns:
            Received data bytes (may be less than buffer_size)
        """
        ...

    async def get_signal_quality(self, interface: str) -> dict[str, Any]:
        """
        Get signal quality metrics for interface.

        Override in wireless/signal-aware adapters.

        Args:
            interface: Interface name to query

        Returns:
            Signal quality metrics dict with keys like:
            - signal_strength: 0-100 percentage
            - noise_level: dBm value
            - snr_db: signal-to-noise ratio
        """
        return {
            "signal_strength": 100,
            "noise_level": 0,
            "snr_db": 99,
        }

    async def get_interface_info(self, interface: str) -> dict[str, Any]:
        """
        Get interface metadata and configuration.

        Override in subclasses to provide detailed interface info.

        Args:
            interface: Interface name to query

        Returns:
            Interface info dict with keys like:
            - speed_mbps: link speed
            - mtu: maximum transmission unit
            - duplex: 'full', 'half', 'simplex'
            - driver: driver name
            - firmware: firmware version
        """
        return {}

    def is_available(self) -> bool:
        """
        Quick check if this adapter family could work on this system.

        Check for required drivers, tools, or hardware.

        Returns:
            True if adapter family has potential to work on this system
        """
        return True

    async def health_check(self) -> dict[str, Any]:
        """
        Perform health check on adapter.

        Returns:
            Health status dict with operational status and diagnostics
        """
        return {
            "status": "healthy",
            "family": self.family,
            "available": self.is_available(),
        }
