"""
SSDP responder — answers M-SEARCH queries on the LAN multicast group.

Why we need this in addition to UDP broadcast + mDNS
----------------------------------------------------
* Some guest/corporate WiFi networks drop 255.255.255.255 broadcast but
  still forward multicast on the well-known SSDP group 239.255.255.250.
* Windows, macOS, and many IoT devices speak SSDP natively, so this
  gives Helen a wider reachability surface with minimal code.
* We advertise a **Helen-specific** service type (``urn:helen-server:
  service:helen:1``), *not* the generic UPnP ``InternetGatewayDevice``
  namespace — so we never confuse ourselves with the local router's
  UPnP stack.

Wire protocol (RFC 9628 / SSDP draft):
    ─────────────── Client M-SEARCH (multicast UDP 1900 → 239.255.255.250:1900)
    M-SEARCH * HTTP/1.1
    HOST: 239.255.255.250:1900
    MAN: "ssdp:discover"
    MX: 2
    ST: urn:helen-server:service:helen:1
    \r\n
    ─────────────── Our response (unicast UDP back to the sender)
    HTTP/1.1 200 OK
    CACHE-CONTROL: max-age=120
    LOCATION: http://<lan-ip>:3000/api/discovery
    SERVER: Helen/1.0 UPnP/1.1
    ST: urn:helen-server:service:helen:1
    USN: uuid:<server_id>::urn:helen-server:service:helen:1
    \r\n

We don't *originate* NOTIFY frames — Helen already announces itself via
mDNS and UDP broadcast, and adding NOTIFY would just spam the network.
SSDP here is a pure responder.
"""

from __future__ import annotations

import asyncio
import socket
import struct
from typing import Any

from app.core.config import get_settings
from app.core.logging import get_logger
from app.services.discovery_service import get_server_id, get_lan_ip

logger = get_logger(__name__)
settings = get_settings()


SSDP_MULTICAST_ADDR = "239.255.255.250"
SSDP_PORT = 1900

HELEN_ST = "urn:helen-server:service:helen:1"

# Common wildcard STs that real-world clients emit. We also respond to
# these so an unmodified SSDP explorer tool can see Helen.
_ACCEPTED_STS = frozenset({
    HELEN_ST,
    "ssdp:all",
    "upnp:rootdevice",
})


class SsdpResponder:
    """Background asyncio task that listens on 239.255.255.250:1900 and
    replies to M-SEARCH frames whose ``ST`` matches Helen or a wildcard."""

    def __init__(self) -> None:
        self._transport: asyncio.DatagramTransport | None = None
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._task is not None:
            return

        loop = asyncio.get_event_loop()

        # DatagramProtocol is simpler than a raw socket for multicast join.
        responder = self
        lan_ip = get_lan_ip() or "0.0.0.0"

        class _Proto(asyncio.DatagramProtocol):
            def connection_made(self, transport: asyncio.DatagramTransport) -> None:
                self.transport = transport

            def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
                loop.create_task(responder._handle(data, addr))

            def error_received(self, exc: Exception) -> None:
                logger.warning("ssdp_recv_error", error=str(exc))

        # Raw socket for multicast membership — asyncio's create_datagram_endpoint
        # won't join a group for us.
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except OSError:
                    pass
            sock.bind(("0.0.0.0", SSDP_PORT))
            # Join 239.255.255.250 on all interfaces. We bind to 0.0.0.0 so
            # we receive on whichever NIC the query arrives.
            mreq = struct.pack(
                "=4s4s",
                socket.inet_aton(SSDP_MULTICAST_ADDR),
                socket.inet_aton(lan_ip),
            )
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            sock.setblocking(False)
        except OSError as e:
            # Port 1900 is often held by the Windows "SSDP Discovery" service.
            # Helen keeps running — UDP broadcast + mDNS still cover discovery.
            logger.warning("ssdp_responder_bind_failed", error=str(e),
                           hint="Windows SSDP Discovery service may own UDP 1900")
            sock.close()
            return

        try:
            transport, _ = await loop.create_datagram_endpoint(
                lambda: _Proto(), sock=sock,
            )
        except Exception as e:
            logger.warning("ssdp_responder_endpoint_failed", error=str(e))
            sock.close()
            return

        self._transport = transport
        logger.info("ssdp_responder_started", group=SSDP_MULTICAST_ADDR,
                    port=SSDP_PORT, lan_ip=lan_ip)

    async def stop(self) -> None:
        if self._transport is not None:
            try:
                self._transport.close()
            except Exception:
                pass
            self._transport = None

    async def _handle(self, data: bytes, addr: tuple[str, int]) -> None:
        # Parse the first line + headers; bail if it's not M-SEARCH.
        try:
            text = data.decode("utf-8", errors="replace")
        except Exception:
            return
        lines = text.split("\r\n")
        if not lines:
            return
        if not lines[0].upper().startswith("M-SEARCH"):
            return

        headers: dict[str, str] = {}
        for line in lines[1:]:
            if not line or ":" not in line:
                continue
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()

        st = headers.get("st", "")
        if st not in _ACCEPTED_STS:
            return

        lan_ip = get_lan_ip() or "127.0.0.1"
        port = settings.PORT
        server_id = get_server_id()
        location = f"http://{lan_ip}:{port}/api/discovery"
        usn = f"uuid:{server_id}::{HELEN_ST}"

        reply = (
            "HTTP/1.1 200 OK\r\n"
            "CACHE-CONTROL: max-age=120\r\n"
            f"LOCATION: {location}\r\n"
            "SERVER: Helen/1.0 UPnP/1.1\r\n"
            f"ST: {HELEN_ST}\r\n"
            f"USN: {usn}\r\n"
            f"X-HELEN-LAN-IP: {lan_ip}\r\n"
            f"X-HELEN-PORT: {port}\r\n"
            "\r\n"
        ).encode("ascii", errors="replace")

        if self._transport is None:
            return
        try:
            # Respond unicast back to the querier, not to the multicast group.
            self._transport.sendto(reply, addr)
        except Exception as e:
            logger.debug("ssdp_reply_send_failed", error=str(e))


ssdp_responder = SsdpResponder()
