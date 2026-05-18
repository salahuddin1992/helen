"""
Helen-NTP — minimal SNTP server for time-isolated LANs.

Air-gapped LANs can't reach pool.ntp.org. Without a time anchor,
JWT iat/exp drift, log timestamps go out of order, and TLS cert
validity windows fail. Helen-NTP serves the host's local clock as
an authoritative SNTPv3/v4 source so every Helen client/server can
``ntpdate helen-server`` and stay in sync.

Wire shape
----------
RFC 5905 SNTP packet (48 bytes):

  | LI(2) | VN(3) | Mode(3) |   Stratum(8)   | Poll(8) | Precision(8) |
  |                          Root Delay (32)                          |
  |                       Root Dispersion (32)                        |
  |                       Reference ID (32)                           |
  |                  Reference Timestamp (64)                         |
  |                  Originate Timestamp (64)                         |
  |                   Receive Timestamp (64)                          |
  |                  Transmit Timestamp (64)                          |

We answer with stratum 1, ref-id "HELN", and the host clock filled
into the receive + transmit fields. That's enough for every
SNTP-class client — chrony, w32time, nodejs ntp libs, etc.

Pure asyncio, UDP only. No PTP, no NTPv4 server-to-server peering.
"""

from __future__ import annotations

import asyncio
import os
import struct
import time
from typing import Optional


# Number of seconds between 1900-01-01 (NTP epoch) and 1970-01-01 (Unix)
NTP_EPOCH_OFFSET = 2_208_988_800


def _to_ntp_timestamp(t: float) -> tuple[int, int]:
    """Convert a Unix-epoch float to an NTP (seconds, fraction) pair."""
    ntp_seconds = int(t) + NTP_EPOCH_OFFSET
    fraction = int((t - int(t)) * 2 ** 32) & 0xFFFFFFFF
    return ntp_seconds, fraction


class HelenNTPServer:
    REFERENCE_ID = b"HELN"     # ASCII tag visible to clients

    def __init__(
        self,
        bind_host: str = "0.0.0.0",
        bind_port: int = 123,
        stratum: int = 2,        # 1 = primary, 2 = secondary; LAN host clocks aren't atomic
    ) -> None:
        self.bind_host = bind_host
        self.bind_port = bind_port
        self.stratum = stratum
        self._transport: Optional[asyncio.DatagramTransport] = None

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._transport, _ = await loop.create_datagram_endpoint(
            lambda: _NTPProtocol(self),
            local_addr=(self.bind_host, self.bind_port),
        )

    async def stop(self) -> None:
        if self._transport:
            self._transport.close()
            self._transport = None

    def build_response(self, query: bytes) -> bytes:
        if len(query) < 48:
            return b""
        # The originate timestamp is the client's transmit timestamp
        # — copy it into our reply unchanged.
        client_tx = query[40:48]

        # Receive timestamp: now (the moment the packet hit our socket)
        recv = time.time()
        recv_s, recv_f = _to_ntp_timestamp(recv)

        # Transmit timestamp: now (just before send)
        tx = time.time()
        tx_s, tx_f = _to_ntp_timestamp(tx)

        # Reference timestamp: the host's last known tick before now
        ref = recv
        ref_s, ref_f = _to_ntp_timestamp(ref)

        # Header byte: LI=0 (no warning), VN=4, Mode=4 (server)
        header = (0 << 6) | (4 << 3) | 4

        # Pack RFC 5905 SNTP response
        return struct.pack(
            "!BBBb11I",
            header,
            self.stratum,
            10,           # Poll interval (2^10 = 1024 s)
            -20,          # Precision (~1 µs)
            0, 0,         # Root delay (32-bit fixed-point)
            0, 0,         # Root dispersion
            int.from_bytes(self.REFERENCE_ID, "big"),
            ref_s, ref_f,
            int.from_bytes(client_tx[:4], "big"),  # Originate s
            int.from_bytes(client_tx[4:], "big"),  # Originate f
        ) + struct.pack("!II II", recv_s, recv_f, tx_s, tx_f)


class _NTPProtocol(asyncio.DatagramProtocol):
    def __init__(self, server: HelenNTPServer) -> None:
        self.server = server
        self.transport: Optional[asyncio.DatagramTransport] = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data: bytes, addr):
        try:
            reply = self.server.build_response(data)
            if reply and self.transport:
                self.transport.sendto(reply, addr)
        except Exception:
            return


# ── Standalone runner ──────────────────────────────────────────────


async def main() -> None:
    bind_host = os.environ.get("HELEN_NTP_HOST", "0.0.0.0")
    bind_port = int(os.environ.get("HELEN_NTP_PORT", "123"))
    stratum = int(os.environ.get("HELEN_NTP_STRATUM", "2"))

    server = HelenNTPServer(bind_host=bind_host, bind_port=bind_port,
                              stratum=stratum)
    await server.start()
    print(f"Helen-NTP listening on {bind_host}:{bind_port} "
          f"(stratum {stratum})")
    try:
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        await server.stop()


if __name__ == "__main__":
    asyncio.run(main())
