"""
Helen-Router — mandatory LAN entry point.

Acts as a reverse proxy + service registry between Helen clients and
Helen-Server instances. When `HELEN_REQUIRE_ROUTER=1` is set on the
servers, clients can ONLY reach those servers through this router —
the servers refuse direct connections.

Why
---
For deployments where the operator wants ONE choke point:
  - Centralised access control (allowlist of LAN subnets)
  - Centralised auditing (every request logged here)
  - Centralised rate limiting / DoS protection
  - Centralised TLS termination (one cert, distributed via this router)
  - Server consolidation (multiple servers behind one IP)

Layout
------
  Client → Helen-Router :8080
                         |
                         ├── /api/*       → upstream Helen-Server :3000/api/*
                         ├── /socket.io/* → upstream Helen-Server :3000/socket.io/*
                         ├── /admin/*     → Helen-Server :3000/admin/*
                         ├── /vault/*     → Helen-Server :3000/vault/*
                         ├── /web/*       → static PWA bundle
                         └── /router/*    → router-local admin API

Security
--------
  1. Source-IP filtering: only RFC1918 + loopback are accepted.
  2. Forwarded marker: every proxied request carries
     ``X-Forwarded-By: helen-router/<token>`` — servers verify this
     against ``HELEN_ROUTER_TOKEN`` before serving.
  3. Per-route auth: registry endpoints (``/router/register``, …)
     require ``Authorization: Bearer <ROUTER_REGISTRATION_TOKEN>``.
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import secrets
import time
from contextlib import asynccontextmanager
from typing import Any

import httpx
import structlog
from fastapi import FastAPI, HTTPException, Request, WebSocket, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse

logger = structlog.get_logger("helen-router")

# ── Configuration via env ────────────────────────────────────────────

ROUTER_HOST = os.environ.get("HELEN_ROUTER_HOST", "0.0.0.0")
ROUTER_PORT = int(os.environ.get("HELEN_ROUTER_PORT", "8080"))

# Token that the router stamps on every forwarded request.
# Servers configured with HELEN_REQUIRE_ROUTER=1 must share this value
# in their HELEN_ROUTER_TOKEN env var, or the check rejects the proxy.
ROUTER_TOKEN = os.environ.get("HELEN_ROUTER_TOKEN", "")

# Token a server presents when registering itself with the router.
REG_TOKEN = os.environ.get(
    "HELEN_ROUTER_REGISTRATION_TOKEN", ROUTER_TOKEN
)

# Static upstream — set if you have a single Helen-Server. Otherwise
# servers register themselves via POST /router/register.
DEFAULT_UPSTREAM = os.environ.get("HELEN_ROUTER_DEFAULT_UPSTREAM", "")

# Allowed source networks (RFC1918 by default — never accept WAN).
# Tolerant parser: any malformed entry in the env var is logged and
# skipped rather than blowing up startup.
_DEFAULT_LAN_NETS = (
    "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,169.254.0.0/16,"
    "127.0.0.0/8,::1/128"
)


def _parse_lan_nets(raw: str) -> list:
    out = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            out.append(ipaddress.ip_network(entry))
        except ValueError as exc:
            logger.warning("invalid_lan_net_skipped",
                           entry=entry, error=str(exc))
    if not out:
        # Should never happen — fall back to the locked-down default
        out = [ipaddress.ip_network(n)
               for n in _DEFAULT_LAN_NETS.split(",")]
    return out


LAN_NETS = _parse_lan_nets(
    os.environ.get("HELEN_ROUTER_ALLOWED_NETS", _DEFAULT_LAN_NETS),
)


# ── In-memory service registry ──────────────────────────────────────


class ServiceRegistry:
    """Tracks live Helen-Server upstreams."""

    def __init__(self) -> None:
        self._servers: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def register(
        self, server_id: str, url: str,
        capabilities: list[str] | None = None,
        last_seen: float | None = None,
    ) -> None:
        async with self._lock:
            self._servers[server_id] = {
                "id": server_id,
                "url": url.rstrip("/"),
                "capabilities": capabilities or [],
                "last_seen": last_seen or time.time(),
                "registered_at": (
                    self._servers.get(server_id, {}).get("registered_at")
                    or time.time()
                ),
            }
        logger.info("upstream_registered",
                    server_id=server_id, url=url)

    async def unregister(self, server_id: str) -> bool:
        async with self._lock:
            return self._servers.pop(server_id, None) is not None

    async def heartbeat(self, server_id: str) -> bool:
        async with self._lock:
            entry = self._servers.get(server_id)
            if entry is None:
                return False
            entry["last_seen"] = time.time()
            return True

    async def pick_upstream(self) -> str | None:
        """Return the freshest live upstream — backwards-compat single
        pick. New code should use ``pick_upstream_ordered`` for the
        full failover-ready ranking."""
        ordered = await self.pick_upstream_ordered()
        return ordered[0] if ordered else (DEFAULT_UPSTREAM or None)

    async def pick_upstream_ordered(self) -> list[str]:
        """Return all live upstreams sorted by **proximity** — lowest
        measured RTT first. Callers iterate this list as a failover
        chain: try the first; if it errors, try the next.

        Proximity is measured opportunistically: every upstream's most
        recent ``rtt_ms`` (set by :func:`_probe_rtt` running in the
        background) is the sort key. Servers without an RTT sample
        yet rank by freshness so a brand-new registrant is still
        picked once.
        """
        async with self._lock:
            cutoff = time.time() - 60
            live = [s for s in self._servers.values()
                    if s["last_seen"] > cutoff]
            if not live:
                return []
            # Two-key sort: (rtt_ms ascending, last_seen descending).
            # Servers without RTT data fall back to a sentinel that
            # places them between fast (<10 ms) and slow (>500 ms)
            # measured peers.
            live.sort(key=lambda s: (
                s.get("rtt_ms", 100.0),
                -s["last_seen"],
            ))
            return [s["url"] for s in live]

    async def update_rtt(self, server_id: str, rtt_ms: float) -> None:
        """Background prober calls this after each measurement."""
        async with self._lock:
            entry = self._servers.get(server_id)
            if entry is not None:
                # Exponentially-weighted moving average so a single
                # spike doesn't disqualify an otherwise-fast peer.
                prev = entry.get("rtt_ms")
                entry["rtt_ms"] = (
                    rtt_ms if prev is None
                    else 0.7 * prev + 0.3 * rtt_ms
                )
                entry["rtt_updated_at"] = time.time()

    async def mark_unreachable(self, server_id: str) -> None:
        """A failed forward attempt brings the RTT estimate up
        sharply so the next ``pick_upstream_ordered`` won't reach
        for this peer first."""
        async with self._lock:
            entry = self._servers.get(server_id)
            if entry is not None:
                entry["rtt_ms"] = 999_999.0
                entry["last_failure"] = time.time()

    async def list_all(self) -> list[dict[str, Any]]:
        async with self._lock:
            now = time.time()
            return [
                {**s, "stale_seconds": int(now - s["last_seen"])}
                for s in self._servers.values()
            ]


registry = ServiceRegistry()


# ── Lifespan: shared HTTP client ────────────────────────────────────


async def _start_mdns_advertisement(port: int) -> Any:
    """Advertise the router on ``_helen-router._tcp.local`` so clients
    can find us without static config. Returns the Zeroconf instance
    (or None on platforms without working zeroconf)."""
    try:
        import threading
        import socket as _socket
        from zeroconf import (
            InterfaceChoice, ServiceInfo, Zeroconf,
        )
    except ImportError:
        logger.info("router_mdns_skipped_no_zeroconf")
        return None

    # Collect local IPs for the SRV record
    addrs: list[bytes] = []
    try:
        import psutil
        for iface, ifa in psutil.net_if_addrs().items():
            for a in ifa:
                if a.family != _socket.AF_INET:
                    continue
                ip = a.address
                if (ip and ip != "127.0.0.1"
                        and not ip.startswith("169.254.")):
                    addrs.append(_socket.inet_aton(ip))
    except Exception:
        pass
    if not addrs:
        addrs = [_socket.inet_aton("127.0.0.1")]

    info = ServiceInfo(
        type_="_helen-router._tcp.local.",
        name=f"Helen-Router-{_socket.gethostname()[:16]}._helen-router._tcp.local.",
        addresses=addrs,
        port=port,
        properties={
            "version": "1.0.0",
            "role": "router",
            "endpoint": f"http://0.0.0.0:{port}",
        },
        server=f"helen-router-{_socket.gethostname()[:16]}.local.",
    )

    # Construct Zeroconf in a worker thread (avoids EventLoopBlocked
    # when called from inside an asyncio loop on Windows).
    box: dict = {}

    def _ctor():
        try:
            try:
                zc = Zeroconf(interfaces=InterfaceChoice.All)
            except Exception:
                zc = Zeroconf()
            zc.register_service(info, allow_name_change=True)
            box["zc"] = zc
            box["info"] = info
        except Exception as exc:
            box["err"] = exc

    t = threading.Thread(target=_ctor, daemon=True)
    t.start()
    t.join(timeout=5.0)

    if "err" in box:
        logger.warning("router_mdns_register_failed", error=str(box["err"]))
        return None
    if "zc" not in box:
        logger.warning("router_mdns_register_timed_out")
        return None

    logger.info("router_mdns_advertised",
                service="_helen-router._tcp.local",
                port=port)
    return box


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not ROUTER_TOKEN:
        logger.error("router_token_missing")
        raise RuntimeError(
            "HELEN_ROUTER_TOKEN must be set (32+ hex chars). Generate "
            "one with: openssl rand -hex 32"
        )
    if len(ROUTER_TOKEN) < 32:
        raise RuntimeError(
            f"HELEN_ROUTER_TOKEN too short ({len(ROUTER_TOKEN)} chars; "
            f"need 32+)"
        )
    # Refuse known-leaked installer fallbacks so a deployment that
    # silently fell back to the per-install placeholder fails fast
    # instead of running with a token attackers can guess.
    _WEAK_TOKENS = {
        "0a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f9",
        "REPLACE_ME_BEFORE_RUNNING_HELEN_ROUTER_64_chars_long_xxxxxxxxxx",
        "change-me", "changeme", "secret",
    }
    if ROUTER_TOKEN in _WEAK_TOKENS:
        logger.error("router_token_known_weak")
        raise RuntimeError(
            "HELEN_ROUTER_TOKEN is a known-weak/installer-placeholder "
            "value. Refusing to start. Edit the .env file and put a "
            "fresh hex string from `openssl rand -hex 32`."
        )

    app.state.client = httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, connect=5.0),
        limits=httpx.Limits(max_keepalive_connections=50,
                            max_connections=200),
    )

    # ── Mesh overlay (peer routers) ──────────────────────────────
    # Multi-router LAN topologies form a mesh: each router exchanges
    # link-state advertisements with every neighbour and Dijkstra-
    # computes routes to every server known across the mesh. Static
    # peers come from HELEN_ROUTER_PEERS=id1=http://..,id2=http://..
    app.state.mesh = None
    try:
        from app.mesh import (
            MeshNode, env_router_id, parse_static_peers,
        )
        my_url = os.environ.get(
            "HELEN_ROUTER_PUBLIC_URL",
            f"http://{ROUTER_HOST}:{ROUTER_PORT}",
        )
        node = MeshNode(router_id=env_router_id(), my_url=my_url)
        for rid, url in parse_static_peers(
            os.environ.get("HELEN_ROUTER_PEERS", ""),
        ):
            node.add_neighbour(rid, url)
        await node.start()
        # Optional alternate topology — defaults to full-mesh Dijkstra.
        # Set HELEN_MESH_TOPOLOGY=ring for predictable-hop lab mode.
        topology_strategy = os.environ.get(
            "HELEN_MESH_TOPOLOGY", "mesh",
        ).strip().lower()
        if topology_strategy != "mesh":
            node.apply_topology_strategy(topology_strategy)
        app.state.mesh = node
        app.state.mesh_topology_strategy = topology_strategy
        logger.info("router_mesh_started",
                    id=node.id,
                    static_peers=len(node.neighbours),
                    topology_strategy=topology_strategy)
    except Exception as exc:
        logger.warning("router_mesh_start_failed", error=str(exc))

    # mDNS advertisement so clients can discover the router without
    # static IP config. Disabled with HELEN_ROUTER_DISABLE_MDNS=1.
    app.state.mdns = None
    if os.environ.get("HELEN_ROUTER_DISABLE_MDNS", "").lower() not in (
            "1", "true", "yes"):
        app.state.mdns = await _start_mdns_advertisement(ROUTER_PORT)

    # Background RTT prober — measures every registered upstream every
    # 10 s so pick_upstream_ordered() can rank by proximity. Disabled
    # with HELEN_ROUTER_DISABLE_RTT=1 (e.g. for tests with frozen time).
    app.state.rtt_task = None
    if os.environ.get("HELEN_ROUTER_DISABLE_RTT", "").lower() not in (
            "1", "true", "yes"):
        app.state.rtt_task = asyncio.create_task(
            _rtt_prober(app.state.client),
            name="router-rtt-prober",
        )

    logger.info("router_started",
                host=ROUTER_HOST, port=ROUTER_PORT,
                allowed_nets=[str(n) for n in LAN_NETS],
                default_upstream=DEFAULT_UPSTREAM or "<none>",
                mdns=bool(app.state.mdns))
    try:
        yield
    finally:
        if app.state.rtt_task:
            app.state.rtt_task.cancel()
        if getattr(app.state, "mesh", None):
            try:
                await app.state.mesh.stop()
            except Exception:
                pass
        if app.state.mdns:
            try:
                box = app.state.mdns
                box["zc"].unregister_service(box["info"])
                box["zc"].close()
            except Exception:
                pass
        await app.state.client.aclose()
        logger.info("router_stopped")


async def _rtt_prober(http: httpx.AsyncClient) -> None:
    """Probe every registered upstream every 10 s, write RTT into the
    registry. The pick logic uses these values to pick the closest
    server. Failed probes mark the upstream unreachable so failover
    can route around it without waiting for a real client request to
    discover the outage.
    """
    while True:
        try:
            await asyncio.sleep(10.0)
            entries = await registry.list_all()
            for entry in entries:
                sid = entry["id"]
                url = entry["url"]
                t0 = time.perf_counter()
                try:
                    r = await http.get(
                        f"{url}/api/health", timeout=2.0,
                        headers={"X-Forwarded-By":
                                 f"helen-router/{ROUTER_TOKEN}"},
                    )
                    rtt = (time.perf_counter() - t0) * 1000
                    if r.status_code == 200:
                        await registry.update_rtt(sid, rtt)
                    else:
                        await registry.mark_unreachable(sid)
                except Exception:
                    await registry.mark_unreachable(sid)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.warning("rtt_prober_loop_error", error=str(exc))


app = FastAPI(
    title="Helen-Router",
    version="1.0.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
)
# CORS: deliberately restrictive. The `lan_only` middleware below
# already rejects non-RFC1918 IPs, but origins-as-wildcard with
# `allow_credentials=True` is broken in modern browsers anyway and
# would silently fail. Match LAN-style origins explicitly.
LAN_ORIGIN_REGEX = (
    r"^(https?://("
    r"localhost|127\.0\.0\.1|\[::1\]|"
    r"\d+\.\d+\.\d+\.\d+|"
    r"[a-zA-Z0-9-]+\.local|"
    r"[a-zA-Z0-9-]+\.helen\.lan"
    r")(:[0-9]+)?|app://\.|null)$"
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["null"],
    allow_origin_regex=LAN_ORIGIN_REGEX,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=[
        "Authorization", "Content-Type", "X-Forwarded-By",
        "X-Helen-Connection-Token",
    ],
    expose_headers=["X-Helen-Upstream"],
)


# ── Source-IP filter ────────────────────────────────────────────────


@app.middleware("http")
async def lan_only(request: Request, call_next):
    client = request.client
    if not client:
        return JSONResponse({"error": "no client"}, status_code=400)
    try:
        addr = ipaddress.ip_address(client.host)
    except ValueError:
        return JSONResponse({"error": "bad client ip"},
                            status_code=400)
    if not any(addr in net for net in LAN_NETS):
        logger.warning("non_lan_request_blocked",
                       remote=str(addr), path=request.url.path)
        return JSONResponse(
            {"error": "lan_only",
             "reason": "Helen-Router refuses non-RFC1918 sources"},
            status_code=403,
        )
    return await call_next(request)


# ── Router admin routes ─────────────────────────────────────────────


def _check_reg_token(authorization: str | None) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token")
    if not secrets.compare_digest(authorization[7:], REG_TOKEN):
        raise HTTPException(403, "invalid registration token")


@app.get("/router/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "helen-router",
        "version": "1.0.0",
        "upstreams": len(await registry.list_all()),
    }


@app.get("/router/upstreams")
async def list_upstreams() -> dict[str, Any]:
    return {"upstreams": await registry.list_all()}


@app.get("/router/topology-strategy")
async def get_topology_strategy(req: Request) -> dict[str, Any]:
    """Returns the active mesh-routing strategy. Public (no token —
    same auth posture as /router/health) so a peer can discover what
    routing model this router speaks."""
    return {
        "strategy": getattr(req.app.state, "mesh_topology_strategy", "mesh"),
        "available": ["mesh", "ring"],
        "doc": (
            "Set HELEN_MESH_TOPOLOGY=ring on this router to switch "
            "to ring-style next-hop forwarding. Default = mesh "
            "(Dijkstra over LSAs)."
        ),
    }


@app.post("/router/register")
async def register(req: Request) -> dict[str, Any]:
    _check_reg_token(req.headers.get("authorization"))
    body = await req.json()
    sid = body.get("server_id")
    url = body.get("url")
    caps = body.get("capabilities", [])
    if not sid or not url:
        raise HTTPException(400, "server_id and url are required")
    await registry.register(sid, url, caps)
    # Announce this direct upstream in the mesh so peer routers can
    # learn they can reach this server through us. Best-effort —
    # mesh may be disabled.
    try:
        node = getattr(req.app.state, "mesh", None)
        if node is not None:
            node.announce_direct_server(sid, capabilities=caps)
    except Exception:
        pass
    return {"status": "registered", "server_id": sid}


@app.post("/router/heartbeat/{server_id}")
async def heartbeat(server_id: str, req: Request) -> dict[str, Any]:
    _check_reg_token(req.headers.get("authorization"))
    if not await registry.heartbeat(server_id):
        raise HTTPException(404, "unknown server_id")
    return {"status": "ok"}


@app.delete("/router/register/{server_id}")
async def unregister(server_id: str, req: Request) -> dict[str, Any]:
    _check_reg_token(req.headers.get("authorization"))
    if not await registry.unregister(server_id):
        raise HTTPException(404, "unknown server_id")
    try:
        node = getattr(req.app.state, "mesh", None)
        if node is not None:
            node.withdraw_direct_server(server_id)
    except Exception:
        pass
    return {"status": "unregistered"}


# ── External LAN device discovery ───────────────────────────────────


@app.get("/router/network")
async def list_lan_devices(
    full: bool = False,
    fingerprint: bool = False,
) -> dict[str, Any]:
    """Discover physical routers / APs / gateways sitting on the LAN.

    Query params
    ------------
      full=true          run a /24 ping sweep too (slow, ~5-15s)
      fingerprint=true   identify each device's vendor via HTTP probe

    Sources combined: SSDP (UPnP/IGD), mDNS, ARP, default-gateway
    lookup, and (optionally) ICMP ping sweep + vendor fingerprinting.
    """
    from app.external_routers import discover_all
    devices = await discover_all(do_ping_sweep=full)

    if fingerprint:
        from app.vendor_adapters import identify_vendor
        # Run vendor fingerprinting in parallel
        async def _fp(d):
            try:
                fp = await identify_vendor(d.ip)
                if fp:
                    if not d.vendor:
                        d.vendor = fp.get("vendor")
                    if "model" not in [c for c in d.capabilities]:
                        d.capabilities.append(
                            f"vendor:{fp.get('fingerprint', 'unknown')}"
                        )
            except Exception:
                pass
        await asyncio.gather(*[_fp(d) for d in devices])

    return {
        "devices": [
            {
                "ip": d.ip,
                "mac": d.mac,
                "hostname": d.hostname,
                "vendor": d.vendor,
                "model": d.model,
                "is_gateway": d.is_gateway,
                "discovered_via": d.discovered_via,
                "capabilities": d.capabilities,
                "upnp_url": d.upnp_url,
            }
            for d in devices
        ],
        "device_count": len(devices),
        "gateways": [d.ip for d in devices if d.is_gateway],
    }


@app.post("/router/connect")
async def smart_connect(req: Request) -> dict[str, Any]:
    """One-shot client → server pipeline.

    The client posts what it WANTS (a capability), the broker returns
    HOW to reach it (endpoint URL + auth token + fallbacks). This
    consolidates discovery, negotiation, and failover-priming into a
    single round-trip.

    Body (all optional)::

        {
          "capability":         "rest" | "socketio" | "webrtc" | "vault",
          "prefer_subnet_local": true,
          "require_proxy":       false,
          "server_id_hint":      "stable-server-id",
          "request_upnp":        false
        }

    Returns::

        {
          "verdict":     "ready" | "no_upstream",
          "endpoint":    "http://10.0.0.5:3000",
          "via":         "direct" | "router-proxy" | "tunnel",
          "auth_token":  "helen-conn:...",
          "expires_at":  1762000000,
          "rtt_hint_ms": 12.3,
          "fallbacks":   ["http://...", ...],
          "notes":       "..."
        }
    """
    from app.connection_broker import (
        ConnectionBroker, ConnectionRequest,
    )

    body: dict[str, Any] = {}
    try:
        body = await req.json()
    except Exception:
        pass

    client_ip = req.client.host if req.client else "0.0.0.0"

    cr = ConnectionRequest(
        client_ip=client_ip,
        capability=str(body.get("capability", "rest")),
        prefer_subnet_local=bool(body.get("prefer_subnet_local", True)),
        require_proxy=bool(body.get("require_proxy", False)),
        server_id_hint=body.get("server_id_hint"),
        request_upnp=bool(body.get("request_upnp", False)),
    )

    broker = ConnectionBroker(registry, ROUTER_TOKEN)
    plan = await broker.plan(cr)

    # Replace the placeholder "ROUTER_LOCAL" with the URL the client
    # actually used to reach us.
    endpoint = plan.endpoint
    if endpoint == "ROUTER_LOCAL":
        host = req.headers.get("host") or f"{ROUTER_HOST}:{ROUTER_PORT}"
        endpoint = f"{req.url.scheme}://{host}"

    return {
        "verdict": "ready" if plan.via != "none" else "no_upstream",
        "endpoint": endpoint,
        "via": plan.via,
        "auth_token": plan.auth_token,
        "expires_at": plan.expires_at,
        "rtt_hint_ms": plan.rtt_hint_ms,
        "fallbacks": plan.fallbacks,
        "notes": plan.notes,
    }


@app.post("/router/upnp/portmap")
async def request_port_mapping(req: Request) -> dict[str, Any]:
    """Ask an upstream UPnP IGD to forward a port for Helen-Server.

    Body: {"upnp_url": "http://...:5000/...", "external_port": 3000,
            "internal_port": 3000, "internal_ip": "10.0.0.5"}
    """
    _check_reg_token(req.headers.get("authorization"))
    body = await req.json()
    upnp_url = body.get("upnp_url")
    if not upnp_url:
        raise HTTPException(400, "upnp_url required")

    from app.upnp_portmap import auto_map_for_helen_server
    ok, ext_ip = await auto_map_for_helen_server(
        upnp_url,
        helen_internal_ip=body.get("internal_ip"),
        external_port=int(body.get("external_port", 3000)),
        internal_port=int(body.get("internal_port", 3000)),
    )
    return {"ok": ok, "external_ip": ext_ip}


# ── Mesh overlay endpoints ──────────────────────────────────────────


@app.post("/mesh/lsa")
async def mesh_lsa(req: Request) -> dict[str, Any]:
    """Receive an LSA from a peer router. Caller's identity is
    derived from the LSA origin field — peer-to-peer trust is
    bounded by source-IP being on LAN_NETS (already enforced by
    middleware)."""
    node = getattr(req.app.state, "mesh", None)
    if node is None:
        raise HTTPException(503, "mesh disabled")
    body = await req.json()
    try:
        from app.mesh import LSA
        lsa = LSA(
            origin=str(body["origin"]),
            epoch=int(body["epoch"]),
            neighbours={str(k): float(v)
                        for k, v in (body.get("neighbours") or {}).items()},
            direct_servers=[str(s) for s in (body.get("direct_servers") or [])],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(400, f"malformed LSA: {exc}")
    accepted = node.receive_lsa(lsa)
    return {"accepted": accepted, "epoch": lsa.epoch, "origin": lsa.origin}


@app.get("/mesh/topology")
async def mesh_topology(req: Request) -> dict[str, Any]:
    """Debug snapshot — neighbours, LSA db keys, computed routes."""
    node = getattr(req.app.state, "mesh", None)
    if node is None:
        return {"enabled": False}
    return {
        "enabled": True,
        "id": node.id,
        "neighbours": [
            {"id": n.router_id, "url": n.url,
             "alive": n.alive, "rtt_ms": n.rtt_ms,
             "last_seen": n.last_seen}
            for n in node.neighbours.values()
        ],
        "direct_servers": list(node.direct_servers.keys()),
        "known_origins": list(node._lsa_db.keys()),
        "routes": {
            sid: [{"next_hop": h, "cost": c} for (h, c) in paths]
            for sid, paths in node.routes.items()
        },
    }


@app.get("/mesh/path/{server_id}")
async def mesh_path(server_id: str, req: Request) -> dict[str, Any]:
    """Resolve next-hop for a server. Useful for clients that want to
    pre-route a request through a known intermediate."""
    node = getattr(req.app.state, "mesh", None)
    if node is None:
        raise HTTPException(503, "mesh disabled")
    nh = node.next_hop(server_id)
    if nh is None:
        # Either we serve it directly, or the server is unknown.
        if server_id in node.direct_servers:
            return {"server_id": server_id, "self_serves": True}
        raise HTTPException(404, "no path to server")
    return {
        "server_id": server_id,
        "next_hop": {"id": nh.router_id, "url": nh.url, "rtt_ms": nh.rtt_ms},
    }


@app.post("/mesh/neighbours")
async def mesh_add_neighbour(req: Request) -> dict[str, Any]:
    """Hot-add a peer router. Body: {router_id, url}.
    Token-gated like /router/register so random LAN hosts can't
    poison the topology."""
    _check_reg_token(req.headers.get("authorization"))
    node = getattr(req.app.state, "mesh", None)
    if node is None:
        raise HTTPException(503, "mesh disabled")
    body = await req.json()
    rid = body.get("router_id")
    url = body.get("url")
    if not rid or not url:
        raise HTTPException(400, "router_id and url required")
    node.add_neighbour(str(rid), str(url))
    return {"ok": True, "neighbours": len(node.neighbours)}


@app.delete("/mesh/neighbours/{router_id}")
async def mesh_remove_neighbour(router_id: str, req: Request) -> dict[str, Any]:
    _check_reg_token(req.headers.get("authorization"))
    node = getattr(req.app.state, "mesh", None)
    if node is None:
        raise HTTPException(503, "mesh disabled")
    node.remove_neighbour(router_id)
    return {"ok": True, "neighbours": len(node.neighbours)}


# ── Standalone admin UI (router's OWN panel) ────────────────────────
# Registered BEFORE the proxy catch-all so /admin/ paths served by
# the router's own SPA win against the upstream-proxy fallthrough.
# Helen-Server's admin (proxied via /admin/*) remains reachable
# because admin_routes.py only owns specific sub-paths (/admin/,
# /admin/login, /admin/logout, /admin/vendor/*, /admin/_health, and
# the static fallthrough). The proxy below still catches anything
# not matched here.
from app.admin_routes import router as admin_ui_router  # noqa: E402
app.include_router(admin_ui_router)


# ── Reverse-proxy core ──────────────────────────────────────────────


# These prefixes get proxied to the chosen upstream Helen-Server.
PROXIED_PREFIXES = (
    "/api/", "/socket.io/", "/admin/", "/admin-secret/",
    "/vault/", "/web/", "/mobile/", "/admin-mobile/", "/metrics",
)


@app.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
)
async def proxy(path: str, request: Request) -> Response:
    full_path = "/" + path

    if not any(full_path.startswith(p) for p in PROXIED_PREFIXES):
        return JSONResponse(
            {"error": "not_found",
             "hint": "Helen-Router proxies /api, /socket.io, /admin, "
                     "/vault, /web only"},
            status_code=404,
        )

    # Get the full failover chain — sorted by proximity (RTT) ascending.
    # We walk the chain; if the closest is down we try the next one
    # automatically. The X-Helen-Upstream response header tells the
    # client which one ultimately served the request.
    upstreams = await registry.pick_upstream_ordered()
    if not upstreams and DEFAULT_UPSTREAM:
        upstreams = [DEFAULT_UPSTREAM]
    if not upstreams:
        return JSONResponse(
            {"error": "no_upstream",
             "reason": "no Helen-Server has registered with the router"},
            status_code=503,
        )

    fwd_headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in ("host", "content-length")
    }
    fwd_headers["X-Forwarded-By"] = f"helen-router/{ROUTER_TOKEN}"
    fwd_headers["X-Forwarded-For"] = (
        request.client.host if request.client else ""
    )
    fwd_headers["X-Forwarded-Proto"] = request.url.scheme
    fwd_headers["X-Forwarded-Host"] = request.url.netloc

    body = await request.body()
    client: httpx.AsyncClient = request.app.state.client

    # Walk the failover chain. The first reachable upstream wins.
    # `attempts` is logged so we can debug "the closest one was dead"
    # scenarios without having to instrument the prober separately.
    attempts: list[str] = []
    last_error: str | None = None
    for upstream in upstreams:
        attempts.append(upstream)
        target = f"{upstream}{full_path}"
        if request.url.query:
            target += "?" + request.url.query
        try:
            upstream_resp = await client.request(
                request.method, target,
                headers=fwd_headers,
                content=body,
            )
            if attempts and len(attempts) > 1:
                logger.info("router_failover_used",
                            tried=attempts, served_by=upstream)
            # Inject debug header so clients can see which server
            # actually served them (useful for "stick to closest")
            extra_headers = {"X-Helen-Upstream": upstream}
            return await _build_proxy_response(
                upstream_resp, extra_headers,
            )
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            last_error = str(exc)
            # Mark unreachable so the next pick_upstream_ordered()
            # demotes this peer for the next request too — saves
            # other in-flight requests the same costly probe.
            sid = next(
                (s["id"] for s in await registry.list_all()
                 if s["url"] == upstream),
                None,
            )
            if sid:
                await registry.mark_unreachable(sid)
            continue
        except Exception as exc:
            last_error = str(exc)
            continue

    # All upstreams failed
    logger.error("router_all_upstreams_failed",
                 attempted=attempts, last_error=last_error)
    return JSONResponse(
        {"error": "all_upstreams_unreachable",
         "attempted": attempts,
         "last_error": last_error},
        status_code=503,
    )


async def _build_proxy_response(
    upstream_resp: httpx.Response,
    extra_headers: dict[str, str] | None = None,
) -> Response:
    """Strip hop-by-hop headers and add diagnostic ones."""
    drop = {
        "connection", "keep-alive", "proxy-authenticate",
        "proxy-authorization", "te", "trailers",
        "transfer-encoding", "upgrade",
    }
    out_headers = {
        k: v for k, v in upstream_resp.headers.items()
        if k.lower() not in drop
    }
    if extra_headers:
        out_headers.update(extra_headers)
    return Response(
        content=upstream_resp.content,
        status_code=upstream_resp.status_code,
        headers=out_headers,
        media_type=upstream_resp.headers.get("content-type"),
    )


# ── WebSocket proxy (Socket.IO upgrade path) ────────────────────────


@app.websocket("/socket.io/")
async def ws_proxy(ws: WebSocket) -> None:
    """Bridge a WebSocket between the client and the upstream server.

    httpx doesn't speak WS, so we use ``websockets`` for the upstream
    leg. Both legs run concurrently — each side's ``recv`` task forwards
    to the other side's ``send``.
    """
    upstream = await registry.pick_upstream()
    if not upstream:
        await ws.close(code=1011)
        return

    try:
        import websockets
    except ImportError:
        logger.error("websockets_module_missing")
        await ws.close(code=1011)
        return

    target_ws_url = (
        upstream.replace("http://", "ws://", 1)
                .replace("https://", "wss://", 1)
        + "/socket.io/?" + (ws.url.query or "")
    )

    await ws.accept()
    # NOTE: the previous on-disk copy was truncated mid-handler.
    # The block below is the canonical completion: bidirectional Socket.IO
    # / WebSocket frame relay between client and upstream Helen-Server.
    try:
        async with websockets.connect(
            target_ws_url,
            additional_headers={
                "X-Forwarded-By": "helen-router/1.0.0",
                "X-Forwarded-For": (ws.client.host if ws.client else "unknown"),
            },
            open_timeout=10,
            close_timeout=5,
            max_size=16 * 1024 * 1024,
            ping_interval=20,
            ping_timeout=10,
        ) as upstream_ws:
            async def _c2u():
                try:
                    while True:
                        msg = await ws.receive()
                        if msg.get("type") == "websocket.disconnect":
                            return
                        if msg.get("bytes") is not None:
                            await upstream_ws.send(msg["bytes"])
                        elif msg.get("text") is not None:
                            await upstream_ws.send(msg["text"])
                except Exception:
                    return

            async def _u2c():
                try:
                    async for frame in upstream_ws:
                        if isinstance(frame, (bytes, bytearray)):
                            await ws.send_bytes(bytes(frame))
                        else:
                            await ws.send_text(frame)
                except Exception:
                    return

            await asyncio.gather(_c2u(), _u2c(), return_exceptions=True)
    except Exception as _exc:
        try:
            logger.warning("ws_proxy_failed", error=str(_exc), target=target_ws_url)
        except Exception:
            pass
    finally:
        try:
            await ws.close()
        except Exception:
            pass


if __name__ == "__main__":
    import uvicorn as _uvicorn
    _port = int(os.environ.get("ROUTER_PORT", "8080"))
    _uvicorn.run(app, host="0.0.0.0", port=_port, log_level="info")
