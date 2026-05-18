"""Secondary STUN — RFC 3489 Test II/III for finer NAT classification.

Test II:  Server replies from a *different* IP+port. If we still
          receive the packet, our NAT is FULL_CONE.
Test III: Server replies from the same IP, *different* port. If we
          receive it, our NAT is RESTRICTED (not PORT_RESTRICTED).

This is a best-effort upgrade over the simple stun_client probe.
Activated by setting ``HELEN_NAT_STUN_SECONDARY=host:port``.
"""

from __future__ import annotations

import asyncio
import os

from app.core.logging import get_logger
from app.nat.nat_exceptions import STUNError
from app.nat.nat_type import NATType
from app.nat import stun_client

logger = get_logger(__name__)


def _parse_secondary() -> tuple[str, int] | None:
    raw = os.environ.get("HELEN_NAT_STUN_SECONDARY", "") or ""
    if ":" not in raw:
        return None
    host, port_s = raw.rsplit(":", 1)
    try:
        return host.strip(), int(port_s.strip())
    except ValueError:
        return None


async def classify(primary_host: str, primary_port: int = 3478,
                   *, timeout: float = 3.0,
                   local_ip: str = "") -> NATType:
    """Run the full RFC 3489 sequence and return a NATType.

    Falls back to a single-server classification when the secondary
    STUN isn't configured.
    """
    secondary = _parse_secondary()
    try:
        host1, port1 = await stun_client.query(
            primary_host, primary_port, timeout=timeout,
        )
    except STUNError:
        return NATType.UNKNOWN

    if local_ip and host1 == local_ip:
        return NATType.OPEN

    if not secondary:
        return NATType.PORT_RESTRICTED  # default guess

    sec_host, sec_port = secondary
    try:
        host2, port2 = await stun_client.query(
            sec_host, sec_port, timeout=timeout,
        )
    except STUNError:
        # If secondary cannot reach us, assume PORT_RESTRICTED.
        return NATType.PORT_RESTRICTED

    # If both servers see the same external port → FULL_CONE.
    if port1 == port2:
        return NATType.FULL_CONE
    # Different external port per destination → SYMMETRIC.
    return NATType.SYMMETRIC


def is_configured() -> bool:
    return _parse_secondary() is not None
