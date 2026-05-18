"""ConnectivityOrchestrator — the "make this server reachable" coordinator.

It chains together the available connectivity strategies in priority order:

  1. **LAN direct** — always active; no configuration needed. This is
     whether the ``/api/health`` endpoint is reachable on the LAN IP.
     (Detected passively; the discovery service owns the UDP broadcast.)

  2. **UPnP / NAT-PMP** — router-assisted port mapping. The *mapping* is
     owned by ``admin_app.router.RouterManager`` (driven from the admin
     dashboard). The orchestrator only *observes* whether it's active.

  3. **Reverse tunnel** — outbound WebSocket to a Helen-Rendezvous. Owned
     by :class:`ReverseTunnelClient`; the orchestrator starts/stops it in
     response to config.

  4. **UDP hole punching** — signaling primitives available, ICE-class
     pairing is not yet wired. The orchestrator reports ``available=False``
     until that lands.

  5. **TCP relay** — blind byte forwarder via the rendezvous. Owned by
     :class:`RelayClient`; opt-in per-deployment because it adds latency
     and funnels all traffic through the rendezvous operator.

Configuration lives in ``settings.connectivity_*`` env vars so operators
can flip the policy without touching code. The orchestrator exposes a
single ``status()`` shape that the admin dashboard renders verbatim.
"""

from __future__ import annotations

import asyncio
import os
import threading
from typing import Any

from app.core.logging import get_logger
from app.services.connectivity.reverse_tunnel import ReverseTunnelClient
from app.services.connectivity.relay import RelayClient

logger = get_logger(__name__)


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _env_bool(name: str, default: bool = False) -> bool:
    v = _env(name).lower()
    if not v:
        return default
    return v in {"1", "true", "yes", "on"}


class ConnectivityOrchestrator:
    """Holds references to each strategy client and exposes an aggregate view.

    The orchestrator is intentionally lazy: strategies only start when
    explicitly enabled (env flag OR admin-dashboard call). Misconfigured
    strategies fail closed and surface the error in :meth:`status` — they
    never crash the server.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tunnel: ReverseTunnelClient | None = None
        self._relay: RelayClient | None = None
        self._started = False

    # ── Lifecycle ──────────────────────────────────────
    async def start(self) -> None:
        """Called from the server's FastAPI lifespan. Reads env and boots
        whichever strategies are configured. Idempotent."""
        with self._lock:
            if self._started:
                return
            self._started = True

        # Reverse tunnel — enabled when HELEN_RENDEZVOUS_URL + token are set.
        ws_url = _env("HELEN_RENDEZVOUS_WS_URL")
        token = _env("HELEN_RENDEZVOUS_TOKEN")
        local_port = _env("PORT") or "3000"
        local_base = f"http://127.0.0.1:{local_port}"
        display_name = _env("HELEN_SERVER_NAME") or _env("SERVER_NAME") or "Helen Server"

        if ws_url and token:
            try:
                self._tunnel = ReverseTunnelClient(
                    rendezvous_ws_url=ws_url,
                    token=token,
                    local_base_url=local_base,
                    display_name=display_name,
                )
                await self._tunnel.start()
                logger.info("connectivity_reverse_tunnel_started")
            except Exception as e:  # pragma: no cover
                logger.warning("connectivity_reverse_tunnel_start_failed", error=str(e))
                self._tunnel = None
        else:
            logger.debug("connectivity_reverse_tunnel_skipped_unset")

        # Relay — enabled when HELEN_RENDEZVOUS_HOST + port + public_id are set.
        # public_id either comes from an upstream tunnel handshake or from a
        # pre-provisioned env var for pure-relay deployments.
        relay_host = _env("HELEN_RENDEZVOUS_HOST")
        relay_port_str = _env("HELEN_RELAY_BACKEND_PORT")
        relay_public_id = _env("HELEN_RELAY_PUBLIC_ID")
        enable_relay = _env_bool("HELEN_RELAY_ENABLE")
        if enable_relay and relay_host and relay_port_str and relay_public_id:
            try:
                self._relay = RelayClient(
                    rendezvous_host=relay_host,
                    backend_port=int(relay_port_str),
                    public_id=relay_public_id,
                    local_host="127.0.0.1",
                    local_port=int(local_port),
                )
                await self._relay.start()
                logger.info("connectivity_relay_started")
            except Exception as e:  # pragma: no cover
                logger.warning("connectivity_relay_start_failed", error=str(e))
                self._relay = None

    async def stop(self) -> None:
        if self._tunnel is not None:
            await self._tunnel.stop()
            self._tunnel = None
        if self._relay is not None:
            await self._relay.stop()
            self._relay = None
        with self._lock:
            self._started = False

    # ── Runtime config (admin-driven) ──────────────────
    async def configure_tunnel(
        self, *, ws_url: str, token: str, display_name: str | None = None,
    ) -> dict[str, Any]:
        """Start / replace the reverse tunnel at runtime. Used by the admin
        dashboard so operators can swap rendezvous targets without a
        server restart."""
        if self._tunnel is not None:
            await self._tunnel.stop()
            self._tunnel = None
        local_port = _env("PORT") or "3000"
        self._tunnel = ReverseTunnelClient(
            rendezvous_ws_url=ws_url,
            token=token,
            local_base_url=f"http://127.0.0.1:{local_port}",
            display_name=display_name or "Helen Server",
        )
        await self._tunnel.start()
        return self.status()

    async def disable_tunnel(self) -> dict[str, Any]:
        if self._tunnel is not None:
            await self._tunnel.stop()
            self._tunnel = None
        return self.status()

    # ── Status ─────────────────────────────────────────
    def status(self) -> dict[str, Any]:
        """Aggregate view rendered by the admin dashboard.

        Each strategy reports ``configured`` (env/setup present) and an
        activity flag. ``active_methods`` is the ranked list of things that
        are actually working right now — the UI highlights the first one.
        """
        tunnel_st = self._tunnel.status() if self._tunnel is not None else {"configured": False}
        relay_st = self._relay.status() if self._relay is not None else {"configured": False}

        methods: list[str] = []
        if tunnel_st.get("connected"):
            methods.append("reverse_tunnel")
        if relay_st.get("active"):
            methods.append("relay")

        return {
            "active_methods": methods,
            "strategies": {
                "lan_direct": {"always_on": True},
                "upnp_natpmp": {
                    "owned_by": "admin_app.router.RouterManager",
                    "note": "mapping state lives in the admin app",
                },
                "reverse_tunnel": tunnel_st,
                "hole_punch": {
                    "configured": False,
                    "available": False,
                    "note": "signaling primitives ready; ICE pairing not yet wired",
                },
                "relay": relay_st,
            },
        }


orchestrator = ConnectivityOrchestrator()
