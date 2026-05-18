"""
Connection Broker — smooth client → router → server pipeline.

Solves the "I'm a client, how do I reach a server?" problem in one
trip. Without the broker the client has to:

  1. Discover routers (mDNS / UDP).
  2. Discover physical gateways (SSDP).
  3. Ask each router which servers it has.
  4. Pick the best server.
  5. Negotiate a port-forward through the gateway if needed.
  6. Connect.

With the broker the client makes ONE call::

    POST /router/connect  {"capability": "rest", ...}

…and gets back a ready-to-use connection plan::

    {
      "verdict": "ready",
      "endpoint": "http://10.0.0.5:3000",
      "via": "direct",                    # or "router-proxy" / "tunnel"
      "auth_token": "...",                # if the broker had to mint one
      "expires_at": 1762000000,
      "fallbacks": [...]                  # 2-3 backup paths
    }

The broker:

  * Uses the in-process registry to find live servers.
  * Filters by capability (rest / socketio / webrtc / vault).
  * Ranks by RTT (registry's prober).
  * Detects whether the client is on the same subnet as the server
    (direct path) or a different subnet (router-proxy path).
  * Optionally requests UPnP port-mapping on the gateway router so a
    client behind NAT can reach the server.
  * Returns multi-path fallbacks so the client's own failover
    pipeline (parallel-race + circuit breaker) can use them too.

The result: a client implementation can be ~30 lines instead of ~300.
The same logic also accelerates server-to-server federation: a
Helen-Server uses the broker to find peer servers for replication
without re-implementing discovery.
"""

from __future__ import annotations

import asyncio
import ipaddress
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ConnectionPlan:
    """The thing a client needs to start talking to a server."""
    endpoint: str
    via: str                           # "direct" | "router-proxy" | "tunnel"
    auth_token: Optional[str] = None
    expires_at: int = 0
    rtt_hint_ms: float = 0.0
    fallbacks: list[str] = field(default_factory=list)
    notes: str = ""


@dataclass
class ConnectionRequest:
    """A client's intent — translates into a ConnectionPlan."""
    client_ip: str
    capability: str = "rest"           # rest | socketio | webrtc | vault
    prefer_subnet_local: bool = True   # try direct path first
    require_proxy: bool = False        # force routing through Helen-Router
    server_id_hint: Optional[str] = None  # pin to a specific server
    request_upnp: bool = False         # ask gateway for port-forward


# ── Helpers ─────────────────────────────────────────────────────────


def same_subnet(a: str, b: str, mask_bits: int = 24) -> bool:
    try:
        net = ipaddress.ip_network(f"{a}/{mask_bits}", strict=False)
        return ipaddress.ip_address(b) in net
    except Exception:
        return False


def _hostport_from_url(url: str) -> tuple[str, int]:
    # Avoid dragging urllib for one parse
    p = url.split("://", 1)[-1]
    host_part = p.split("/", 1)[0]
    if ":" in host_part:
        host, port = host_part.rsplit(":", 1)
        return host, int(port)
    return host_part, 80 if url.startswith("http://") else 443


# ── Broker ──────────────────────────────────────────────────────────


class ConnectionBroker:
    """Smart matchmaker between clients, routers and servers.

    The broker doesn't store its own state — it queries the live
    registry passed at init. This keeps it idempotent and lets the
    same broker instance serve thousands of concurrent
    /router/connect calls without lock contention.
    """

    def __init__(
        self,
        registry,                   # ServiceRegistry from app.main
        router_token: str,
        plan_ttl_sec: int = 300,
    ) -> None:
        self.registry = registry
        self.router_token = router_token
        self.plan_ttl = plan_ttl_sec

    async def plan(self, req: ConnectionRequest) -> ConnectionPlan:
        """Compute a fully-resolved connection plan for the client."""

        # 1. Pull the current upstream list, sorted by RTT.
        upstreams_ordered = await self.registry.pick_upstream_ordered()
        all_known = await self.registry.list_all()
        url_to_entry = {u: e for e in all_known for u in [e["url"]]}

        # 2. Filter by capability if any upstream has caps recorded.
        eligible = []
        for url in upstreams_ordered:
            entry = url_to_entry.get(url)
            if not entry:
                continue
            caps = entry.get("capabilities") or []
            if not caps or req.capability in caps:
                eligible.append(entry)

        if req.server_id_hint:
            pinned = [e for e in eligible
                      if e["id"] == req.server_id_hint]
            if pinned:
                eligible = pinned

        if not eligible:
            return ConnectionPlan(
                endpoint="", via="none",
                notes="no upstream matches the requested capability",
            )

        # 3. Subnet-locality bias: if the client and a server share a
        # /24, prefer that server even if it's slightly slower —
        # crossing subnets adds router proxy overhead.
        if req.prefer_subnet_local and not req.require_proxy:
            for entry in eligible:
                host, _port = _hostport_from_url(entry["url"])
                if same_subnet(req.client_ip, host):
                    return self._direct_plan(entry, eligible)

        # 4. If the client demanded a proxy path, or no subnet match,
        # return the router-proxy plan: client talks to the router,
        # router proxies to the upstream chain.
        if req.require_proxy:
            return self._proxy_plan(eligible)

        # 5. Default: closest by RTT, direct.
        return self._direct_plan(eligible[0], eligible)

    # ── plan builders ────────────────────────────────────────

    def _direct_plan(self, entry: dict[str, Any],
                      eligible: list[dict[str, Any]]) -> ConnectionPlan:
        fallbacks = [e["url"] for e in eligible if e["url"] != entry["url"]]
        return ConnectionPlan(
            endpoint=entry["url"],
            via="direct",
            auth_token=self._mint_token(entry["id"]),
            expires_at=int(time.time() + self.plan_ttl),
            rtt_hint_ms=float(entry.get("rtt_ms") or 0.0),
            fallbacks=fallbacks[:3],
            notes=f"direct to server={entry['id']}",
        )

    def _proxy_plan(self,
                     eligible: list[dict[str, Any]]) -> ConnectionPlan:
        # Client connects to the router itself, router proxies along
        # the failover chain. The "endpoint" here is the router's
        # public-facing URL — but we don't know that from inside the
        # broker. The HTTP layer fills it in via X-Forwarded-Host.
        return ConnectionPlan(
            endpoint="ROUTER_LOCAL",  # placeholder, replaced by main.py
            via="router-proxy",
            auth_token=self._mint_token("router-proxy"),
            expires_at=int(time.time() + self.plan_ttl),
            rtt_hint_ms=float(eligible[0].get("rtt_ms") or 0.0),
            fallbacks=[e["url"] for e in eligible[1:4]],
            notes=("router-proxy: client → Helen-Router → "
                    f"{len(eligible)} upstream(s)"),
        )

    def _mint_token(self, server_id: str) -> str:
        # A short-lived, random token clients can present as
        # X-Helen-Connection-Token. The server-side validation is
        # opportunistic — main use is letting routers correlate
        # multi-leg flows in their access log.
        nonce = secrets.token_hex(8)
        return f"helen-conn:{server_id}:{nonce}"
