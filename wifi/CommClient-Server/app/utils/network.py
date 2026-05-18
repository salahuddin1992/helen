"""
Network utility functions — LAN IP detection, validation.
"""

from __future__ import annotations

import ipaddress
import logging
import socket

logger = logging.getLogger(__name__)


def is_private_ip(ip: str) -> bool:
    """Check if an IP address is in a private/LAN range."""
    try:
        addr = ipaddress.ip_address(ip)
        return addr.is_private
    except ValueError:
        return False


def get_all_lan_ips() -> list[str]:
    """Get all LAN IP addresses of this machine."""
    ips = []
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if is_private_ip(ip) and ip != "127.0.0.1":
                ips.append(ip)
    except (socket.gaierror, OSError) as e:
        logger.debug("get_all_lan_ips: hostname resolution failed: %s", e)
    return ips


def get_primary_lan_ip() -> str:
    """Get the primary LAN IP (the one most likely used for communication)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.1)
        s.connect(("10.254.254.254", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        ips = get_all_lan_ips()
        return ips[0] if ips else "127.0.0.1"
