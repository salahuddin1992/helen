"""TCP relay client — last-resort byte pipe via Helen-Rendezvous.

Flow (see rendezvous ``main.py`` / ``RelayHub``):

  1. Helen-Server opens a TCP connection to ``<rendezvous>:9101``, sends
     ``REGISTER <public_id>\\n``, keeps the socket open.
  2. Rendezvous replies ``OK waiting\\n`` and parks the connection.
  3. External client connects to ``<rendezvous>:9102``, sends
     ``LOOKUP <public_id>\\n``.
  4. Rendezvous replies ``OK joined\\n`` to the client, sends ``GO\\n`` to
     our parked socket, then blindly forwards bytes in both directions.

We then treat the joined socket as if it were a direct TCP connection to an
external client and forward it into the local server's listener (or
multiplex plain-byte protocols on top). For HTTP workloads, the reverse
tunnel (WebSocket) path is cleaner — this relay exists as a guaranteed
fallback for when WebSocket can't be established at all (very restrictive
guest WiFi blocking port 9090, etc.).
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)


class RelayClient:
    def __init__(
        self,
        *,
        rendezvous_host: str,
        backend_port: int,
        public_id: str,
        local_host: str = "127.0.0.1",
        local_port: int = 3000,
    ) -> None:
        self._rendezvous_host = rendezvous_host
        self._backend_port = backend_port
        self._public_id = public_id
        self._local_host = local_host
        self._local_port = local_port
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.last_error: str | None = None
        self.sessions_served: int = 0

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="helen-relay-client")
        logger.info(
            "relay_client_starting", host=self._rendezvous_host,
            port=self._backend_port, public_id=self._public_id,
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        logger.info("relay_client_stopped")

    def status(self) -> dict[str, Any]:
        return {
            "configured": True,
            "active": self._task is not None and not self._task.done(),
            "sessions_served": self.sessions_served,
            "last_error": self.last_error,
        }

    async def _run(self) -> None:
        """Keeps a REGISTER session parked on the rendezvous. When a
        frontend arrives, relay proxies bytes locally and we spin up a new
        parked session. One-connection-at-a-time by design; deploy more
        relay clients for parallelism."""
        while not self._stop.is_set():
            try:
                rr_reader, rr_writer = await asyncio.open_connection(
                    self._rendezvous_host, self._backend_port,
                )
                rr_writer.write(f"REGISTER {self._public_id}\n".encode())
                await rr_writer.drain()

                greet = await rr_reader.readline()
                if not greet.startswith(b"OK"):
                    raise RuntimeError(f"relay rejected us: {greet!r}")

                # Park — wait for the `GO\n` sentinel that tells us a
                # frontend is now glued to the other end of this pipe.
                go = await rr_reader.readline()
                if go.strip() != b"GO":
                    raise RuntimeError(f"unexpected relay handshake: {go!r}")

                local_r, local_w = await asyncio.open_connection(
                    self._local_host, self._local_port,
                )
                self.sessions_served += 1
                logger.info("relay_session_started", session=self.sessions_served)

                # Join rendezvous <→ local forever.
                await _bridge(rr_reader, rr_writer, local_r, local_w)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
                logger.warning("relay_session_error", error=self.last_error)
                await asyncio.sleep(3.0)


async def _bridge(r_a: asyncio.StreamReader, w_a: asyncio.StreamWriter,
                  r_b: asyncio.StreamReader, w_b: asyncio.StreamWriter) -> None:
    async def pipe(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        try:
            while True:
                chunk = await r.read(65536)
                if not chunk:
                    break
                w.write(chunk)
                await w.drain()
        except (asyncio.CancelledError, ConnectionError, OSError):
            pass
        finally:
            with contextlib.suppress(Exception):
                w.close()
    # ``return_exceptions=True`` so one half of the bidirectional pipe
    # failing (peer disconnect, slow drain, OSError) doesn't tear down
    # the other half before it can finish flushing.
    await asyncio.gather(
        pipe(r_a, w_b), pipe(r_b, w_a),
        return_exceptions=True,
    )
