"""
Emergency / Public Safety transport adapter.

Family: ``emergency_public_safety``

Provides detection of emergency_public_safety-class network interfaces by matching
keywords on psutil interface names plus serial-port descriptions.
``connect()`` / ``send()`` / ``receive()`` open a TCP-style stream over
whatever underlying device the OS exposed; for non-IP buses (RS-485,
CAN, audio) it falls back to pyserial when the port matches.
"""

from __future__ import annotations

import asyncio
import socket
from typing import Any

from app.core.logging import get_logger
from app.transports.adapters.base import BaseTransportAdapter

logger = get_logger(__name__)


class EmergencyAdapter(BaseTransportAdapter):
    family = "emergency_public_safety"
    display_name = "Emergency / Public Safety"
    keywords = ['p25', 'tetra', 'dmr', 'fnet', 'first-responder', 'publicsafety']

    async def detect(self) -> list[dict[str, Any]]:
        detected: list[dict[str, Any]] = []

        # 1) IP interfaces whose name hints at this transport family
        try:
            import psutil
            for ifname, addrs in psutil.net_if_addrs().items():
                low = ifname.lower()
                if any(kw in low for kw in self.keywords):
                    ip = next(
                        (a.address for a in addrs if a.family == socket.AF_INET),
                        None,
                    )
                    detected.append({
                        "interface": ifname,
                        "type": self.family,
                        "ip": ip,
                        "status": "available",
                        "metadata": {"medium": self.family, "source": "psutil"},
                    })
        except Exception as exc:
            logger.debug("emergency_public_safety_psutil_detect_failed", error=str(exc))

        # 2) Serial ports whose description hints at this transport
        try:
            import serial.tools.list_ports
            for port, desc, hwid in serial.tools.list_ports.comports():
                low = (desc or "").lower()
                if any(kw in low for kw in self.keywords):
                    detected.append({
                        "interface": port,
                        "port": port,
                        "type": self.family,
                        "description": desc,
                        "status": "available",
                        "metadata": {"medium": self.family, "source": "serial"},
                    })
        except ImportError:
            pass
        except Exception as exc:
            logger.debug("emergency_public_safety_serial_detect_failed", error=str(exc))

        logger.info("emergency_public_safety_detection_complete", count=len(detected))
        return detected

    async def connect(self, interface: str, config: dict[str, Any]) -> Any:
        # Heuristic: COM*/dev/tty* paths go through pyserial; everything
        # else is treated as an IP-bound interface and we open a TCP
        # stream against ``config['host']:config['port']``.
        if interface.upper().startswith("COM") or interface.startswith("/dev/tty"):
            try:
                import serial
                conn = serial.Serial(
                    interface,
                    baudrate=config.get("baudrate", 9600),
                    timeout=config.get("timeout", 1.0),
                )
                logger.info("emergency_public_safety_serial_connected", interface=interface)
                return conn
            except Exception as exc:
                logger.error("emergency_public_safety_serial_connect_failed", error=str(exc))
                raise

        host = config.get("host", "127.0.0.1")
        port = int(config.get("port", 0))
        if not port:
            raise ValueError(f"{self.family} TCP connect requires config['port']")
        reader, writer = await asyncio.open_connection(host, port)
        logger.info("emergency_public_safety_tcp_connected", host=host, port=port)
        return (reader, writer)

    async def disconnect(self, connection_id: Any) -> bool:
        try:
            if isinstance(connection_id, tuple) and len(connection_id) == 2:
                _, writer = connection_id
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
            elif hasattr(connection_id, "close"):
                connection_id.close()
            return True
        except Exception as exc:
            logger.error("emergency_public_safety_disconnect_failed", error=str(exc))
            return False

    async def send(self, connection_id: Any, data: bytes) -> int:
        try:
            if isinstance(connection_id, tuple) and len(connection_id) == 2:
                _, writer = connection_id
                writer.write(data)
                await writer.drain()
                return len(data)
            if hasattr(connection_id, "write"):
                return int(connection_id.write(data) or 0)
            return 0
        except Exception as exc:
            logger.error("emergency_public_safety_send_failed", error=str(exc))
            return 0

    async def receive(
        self, connection_id: Any, buffer_size: int = 65536
    ) -> bytes:
        try:
            if isinstance(connection_id, tuple) and len(connection_id) == 2:
                reader, _ = connection_id
                return await reader.read(buffer_size)
            if hasattr(connection_id, "read"):
                return connection_id.read(buffer_size)
            return b""
        except Exception as exc:
            logger.error("emergency_public_safety_receive_failed", error=str(exc))
            return b""

    def is_available(self) -> bool:
        try:
            import psutil  # noqa: F401
            return True
        except ImportError:
            return False
