"""
Cluster mesh — layered on top of existing UDP discovery.

This module does three things that turn the node registry from a
manual list into a self-healing mesh:

  1. AUTO-SYNC FROM UDP DISCOVERY
     Helen already listens for UDP broadcasts via peer_registry.
     Every ~10s we walk those discovered peers and, for any that aren't
     yet in node_registry, we HTTP-probe their /api/cluster/info to
     learn their capability + load + known peers. Then we register them.

  2. TRANSITIVE DISCOVERY VIA GOSSIP
     The existing gossip payload carries self-load. We enlarge it to
     also include `known_peers` — a compact list of every node this
     server has seen. When peer B receives gossip from A with peer C
     in the list, B can reach C too (even if B and C never saw each
     other directly). This is epidemic broadcast and it converges in
     O(log N) rounds.

  3. TRAFFIC RELAY
     Sometimes A wants to contact C but the direct route is blocked
     (subnet boundary, asymmetric firewall). The mesh lets A ask B:
     "please forward this HTTP request to C and give me back the
     response." No persistent tunnel; just on-demand forwarding.

Scale: this design handles 1000+ nodes on one LAN without O(N²)
communication because each gossip round fans out to K random peers
(K=3), so total messages per round are O(N·K) = O(N) not O(N²). For
much larger fleets (10k+), we'd add a DHT layer for lookup — Helen
already has kademlia in dht_kademlia.py, hooking into it is a future
polish. For LAN scale, random fan-out is plenty.
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any, Optional

import structlog

logger = structlog.get_logger(__name__)

# ── Tunables ────────────────────────────────────────────────────
# All env-overridable. Defaults raised AGAIN for "never give up" mode:
# faster sync (5s vs 10s), wider fanout (K=10 vs 5), bigger gossip
# carry (500 vs 200). With K=10 and a 5s cycle, a 10,000-node mesh
# converges in 4 rounds = 20 seconds; a 1M-node mesh in 6 rounds = 30s.
# Persistent retry handles transient unreachability so a peer that's
# briefly offline is automatically rediscovered without operator action.
import os as _os_mesh
SYNC_INTERVAL_SEC   = max(2, int(_os_mesh.environ.get("HELEN_MESH_SYNC_INTERVAL_SEC", "5")))
PROBE_TIMEOUT_SEC   = float(_os_mesh.environ.get("HELEN_MESH_PROBE_TIMEOUT_SEC", "3.0"))
GOSSIP_FANOUT       = max(2, int(_os_mesh.environ.get("HELEN_GOSSIP_FANOUT", "10")))
MAX_KNOWN_IN_GOSSIP = max(50, int(_os_mesh.environ.get("HELEN_MAX_KNOWN_IN_GOSSIP", "500")))


class ClusterMesh:
    """Singleton runtime for auto-discovery, gossip expansion, and relay."""

    _singleton: "ClusterMesh | None" = None

    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._running = False

    @classmethod
    def instance(cls) -> "ClusterMesh":
        if cls._singleton is None:
            cls._singleton = ClusterMesh()
        return cls._singleton

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run(), name="helen-cluster-mesh")
        logger.info("cluster_mesh_started")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try: await self._task
            except Exception: pass
        logger.info("cluster_mesh_stopped")

    async def _run(self) -> None:
        """Adaptive sync rate.

        First 60 seconds: 2-second cycle. New servers find each other
        within the first minute; rapid sync prevents the "split brain"
        window where two halves of the mesh haven't yet learned about
        each other.

        After 60 seconds: settle to SYNC_INTERVAL_SEC (default 5s) so
        steady-state CPU/network cost is bounded. Persistent retries
        run on this slower cycle — that's fine because by then the
        mesh has converged."""
        import time as _t_run
        start = _t_run.time()
        await asyncio.sleep(3)   # let discovery warm up
        while self._running:
            try:
                await self._sync_once()
            except Exception as e:
                logger.warning("cluster_mesh_sync_failed", error=str(e))
            elapsed = _t_run.time() - start
            interval = 2 if elapsed < 60 else SYNC_INTERVAL_SEC
            await asyncio.sleep(interval)

    # ── Sync: absorb UDP-discovered peers into NodeRegistry ────
    async def _sync_once(self) -> None:
        """Walk the UDP peer list + known_peers from recent gossip.

        For each peer we DON'T already have in NodeRegistry, probe
        http://host:port/api/cluster/info to learn its capability.
        """
        from app.services.peer_registry import peer_registry
        from app.services.node_registry import get_registry

        try:
            import httpx
        except ImportError:
            return

        reg = get_registry()
        known_ids = {n.node_id for n in reg.nodes(include_dead=True)}
        udp_peers = await peer_registry.list(include_stale=False)

        # Newly-seen peers from UDP
        to_probe: list[tuple[str, str, int]] = []
        for p in udp_peers:
            if p.server_id in known_ids:
                continue
            to_probe.append((p.server_id, p.host, p.port))

        # Also absorb any known_peers we learned about transitively via gossip.
        for cand_id, cand_host, cand_port in self._pending_transitive_peers:
            if cand_id not in known_ids:
                to_probe.append((cand_id, cand_host, cand_port))
        self._pending_transitive_peers.clear()

        # Persistent retries — peers we've seen before but couldn't reach.
        # Re-enter the probe queue once their backoff window expires. This
        # is what gives the mesh its "never gives up" property: a peer
        # that's offline now will be reprobed on every cycle until it
        # comes back, with exponentially-spaced attempts so we don't
        # hammer dead peers.
        for sid, host, port in self._drain_retries():
            if sid not in known_ids:
                to_probe.append((sid, host, port))

        if not to_probe:
            return

        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_SEC) as client:
            async def _probe(sid: str, host: str, port: int):
                try:
                    r = await client.get(f"http://{host}:{port}/api/cluster/info")
                    if r.status_code != 200:
                        return
                    d = r.json()
                    reg.register_peer(
                        node_id=sid,
                        host=host,
                        port=port,
                        capability=d.get("capability", {}),
                        roles=d.get("roles", {}),
                        capacity=d.get("capacity", {}),
                    )
                    # Absorb this peer's known_peers recursively (bounded).
                    for kp in d.get("known_peers", [])[:MAX_KNOWN_IN_GOSSIP]:
                        self._enqueue_transitive(kp)
                except Exception as e:
                    logger.debug("mesh_probe_failed",
                                 peer=sid, host=host, port=port, error=str(e))
                    # Persistent-retry: schedule a re-probe rather than
                    # silently dropping the peer.
                    self._schedule_retry(sid, host, port)

            await asyncio.gather(
                *[_probe(sid, h, p) for sid, h, p in to_probe],
                return_exceptions=True,
            )

    # ── Persistent retry tracker ────────────────────────────────
    # When a probe fails, we DO NOT forget the peer — instead we
    # schedule a retry with exponential backoff (capped at 60s) so the
    # mesh keeps trying every known peer forever. Combined with the
    # multi-NIC discovery loop, this means: if a peer is reachable
    # through ANY route eventually, we'll find them.
    #
    # State: peer_id → (host, port, fail_count, next_retry_ts).
    _retry_queue: dict[str, tuple[str, int, int, float]] = {}

    def _schedule_retry(self, sid: str, host: str, port: int) -> None:
        import time as _t
        prev = self._retry_queue.get(sid)
        fail_count = (prev[2] if prev else 0) + 1
        # Exponential backoff: 2, 4, 8, 16, 32, 60, 60, 60... seconds
        wait = min(60.0, 2.0 * (2 ** min(fail_count - 1, 5)))
        self._retry_queue[sid] = (host, port, fail_count, _t.time() + wait)
        logger.debug(
            "cluster_mesh_retry_scheduled",
            peer=sid, host=host, port=port,
            fail_count=fail_count, wait_sec=wait,
        )

    def _drain_retries(self) -> list[tuple[str, str, int]]:
        """Pop all retries whose backoff window has elapsed."""
        import time as _t
        now = _t.time()
        ready: list[tuple[str, str, int]] = []
        for sid, (host, port, _fc, ts) in list(self._retry_queue.items()):
            if ts <= now:
                ready.append((sid, host, port))
                del self._retry_queue[sid]
        return ready

    _pending_transitive_peers: list[tuple[str, str, int]] = []

    def _enqueue_transitive(self, peer: dict) -> None:
        """Queue a peer learned from gossip for probing on the next sync tick."""
        try:
            sid = str(peer.get("node_id", ""))
            host = str(peer.get("host", ""))
            port = int(peer.get("port", 0))
            if sid and host and port:
                self._pending_transitive_peers.append((sid, host, port))
        except Exception:
            pass

    def absorb_gossip_known_peers(self, known_peers: list[dict]) -> None:
        """Called by the gossip receive handler to enqueue transitively-known
        peers. Actual probing happens in the next sync tick, bounded fan-out.
        """
        for p in known_peers[:MAX_KNOWN_IN_GOSSIP]:
            self._enqueue_transitive(p)

    # ── Gossip fan-out: pick K random peers instead of all ────
    def pick_gossip_targets(self, all_peers: list, k: int = GOSSIP_FANOUT) -> list:
        """Select up to K random peers for a gossip round.

        Using random fan-out keeps message count O(N) per round (each node
        sends K, receives ~K on average) regardless of cluster size. A
        message needs O(log_K N) rounds to reach everyone — 3 rounds at
        K=3 covers 27 nodes; 6 rounds covers 729; 9 rounds covers 19k.
        """
        if len(all_peers) <= k:
            return list(all_peers)
        return random.sample(all_peers, k)


def get_mesh() -> ClusterMesh:
    return ClusterMesh.instance()


# ── Traffic relay helper ─────────────────────────────────────────
async def relay_request(
    target_node_id: str,
    method: str,
    path: str,
    body: Optional[dict] = None,
    headers: Optional[dict] = None,
    timeout: float = 10.0,
    hops_remaining: int = 4,
    seen_proxies: Optional[set] = None,
) -> tuple[int, Any, dict]:
    """Forward an HTTP request to another node — recursive relay chain.

    Path-finding strategy (in order):
      1. **Direct** — try ``http://target.host:target.port/path``.
      2. **Bridge** — proxy via peers that advertised ``bridge: true``
         (multi-homed boxes spanning >1 subnet — typical USB-Ethernet
         dongle, dual-NIC, or "WiFi + Ethernet" laptops).
      3. **Any peer** — fall back to up to 5 random peers. Each proxy
         is itself a Helen-Server, so when WE forward to it, IT will
         re-enter `relay_request` recursively (with `hops_remaining-1`)
         to keep the chain going. That's how the relay works through
         a path A→B→C→D where no single link covers the whole path.

    Loop prevention:
      * `hops_remaining` decrements each hop; when it hits 0 we stop.
        Default 4 covers a 5-server chain — enough for any realistic
        LAN-with-bridges deployment.
      * `seen_proxies` carries the ids we've already tried this round
        so a hop never returns to a peer that's already in the path.

    Returns (status_code, json_body_or_text, response_headers).
    """
    if seen_proxies is None:
        seen_proxies = set()
    try:
        import httpx
    except ImportError:
        return 503, {"error": "httpx_missing"}, {}

    from app.services.node_registry import get_registry
    from app.services.path_health import get_path_health
    health = get_path_health()
    reg = get_registry()
    all_nodes = reg.nodes(include_dead=False)
    target = next((n for n in all_nodes if n.node_id == target_node_id), None)
    if not target:
        return 404, {"error": "unknown_node", "node_id": target_node_id}, {}

    url = f"http://{target.host}:{target.port}{path}"
    # Build a list of host candidates. A multi-homed target advertises
    # `host_aliases` — try each interface in turn so a path that's only
    # reachable via the target's secondary IP (e.g. its WiFi instead of
    # Ethernet) still works.
    host_candidates = [target.host]
    try:
        from app.services.peer_registry import peer_registry
        peer = await peer_registry.get(target_node_id)
        if peer and getattr(peer, "host_aliases", None):
            for alias in peer.host_aliases:
                if alias and alias != target.host and alias not in host_candidates:
                    host_candidates.append(alias)
    except Exception:
        pass

    # If we've run out of hop budget, only try direct paths — no recursion.
    if hops_remaining <= 0:
        async with httpx.AsyncClient(timeout=timeout) as client:
            for cand_host in host_candidates:
                if health.is_failed(cand_host, target.port):
                    continue
                t0 = time.time()
                try:
                    cand_url = f"http://{cand_host}:{target.port}{path}"
                    r = await client.request(method, cand_url, json=body, headers=headers or {})
                    health.record_success(cand_host, target.port, (time.time() - t0) * 1000)
                    try: return r.status_code, r.json(), dict(r.headers)
                    except Exception: return r.status_code, r.text, dict(r.headers)
                except Exception:
                    health.record_failure(cand_host, target.port)
                    continue
        return 502, {"error": "hops_exhausted"}, {}

    async with httpx.AsyncClient(timeout=timeout) as client:
        # 1. Try direct on every advertised host alias of the target.
        # Skip aliases that recently failed (path-health TTL cooldown)
        # and order the rest best-first by EWMA latency score.
        live_hosts = [h for h in host_candidates
                      if not health.is_failed(h, target.port)]
        live_hosts.sort(
            key=lambda h: health.latency_score(h, target.port),
            reverse=True,
        )
        last_direct_err: Optional[Exception] = None
        for cand_host in live_hosts:
            t0 = time.time()
            try:
                cand_url = f"http://{cand_host}:{target.port}{path}"
                r = await client.request(method, cand_url, json=body, headers=headers or {})
                health.record_success(cand_host, target.port, (time.time() - t0) * 1000)
                try: return r.status_code, r.json(), dict(r.headers)
                except Exception: return r.status_code, r.text, dict(r.headers)
            except Exception as direct_err:
                health.record_failure(cand_host, target.port)
                last_direct_err = direct_err
                logger.debug("relay_direct_failed",
                             target=target_node_id, host=cand_host,
                             error=str(direct_err))
        if last_direct_err is not None:
            logger.info("relay_direct_all_failed",
                        target=target_node_id,
                        tried_hosts=host_candidates,
                        last_error=str(last_direct_err))

        # 2. Direct failed — recursive relay chain.
        #
        # Each proxy candidate is itself a Helen-Server. When we POST
        # /api/cluster/relay to it, IT runs `relay_request` again on its
        # side, which repeats this whole flow with `hops_remaining-1`.
        # That's how a chain A→B→C→D works:
        #   A sees B as a proxy, B can't reach D directly either, B
        #   sees C as a proxy, C reaches D directly. Each hop adds one
        #   to the path; we cap at `hops_remaining` to prevent loops.
        #
        # Bridge ordering: peers with bridge=True (multi-homed) tried
        # first because they're statistically the highest-probability
        # link to a target on another router.
        candidates = [n for n in all_nodes
                      if n.node_id != target_node_id and not n.self_node
                      and n.node_id not in seen_proxies]

        def _is_bridge(n) -> bool:
            try:
                return bool((n.extra or {}).get("bridge", False))
            except Exception:
                return False

        # Drop proxies that are inside the per-path cooldown so we
        # don't pile retries onto a flapping link.
        candidates = [
            n for n in candidates
            if not health.is_failed(n.host, n.port)
        ]
        # Use the load_balancer to combine latency + trust + headroom +
        # capacity + bridge bonus into a single weight per proxy. This
        # turns the relay chain into a true cluster-aware router rather
        # than a latency-only chooser. Bridges still rise to the top via
        # the +10% W_BRIDGE bonus.
        try:
            from app.services.load_balancer import rank_proxies
            ranked = rank_proxies(candidates, top_k=8)
            ordered = [s.node for s in ranked]
        except Exception:
            # Fallback to latency-only sort if load_balancer fails.
            bridges = [n for n in candidates if _is_bridge(n)]
            non_bridges = [n for n in candidates if not _is_bridge(n)]
            bridges.sort(key=lambda n: health.latency_score(n.host, n.port), reverse=True)
            non_bridges.sort(key=lambda n: health.latency_score(n.host, n.port), reverse=True)
            ordered = bridges + non_bridges
        new_seen = seen_proxies | {n.node_id for n in ordered[:8]}
        for intermediate in ordered[:8]:
            t0 = time.time()
            try:
                proxy_url = f"http://{intermediate.host}:{intermediate.port}/api/cluster/relay"
                r = await client.post(proxy_url, json={
                    "target_node_id": target_node_id,
                    "method": method,
                    "path": path,
                    "body": body,
                    # Tell the proxy how many more hops it has, and
                    # which proxies are already in our chain.
                    "_hops_remaining": hops_remaining - 1,
                    "_seen_proxies": list(new_seen),
                })
                if r.status_code == 200:
                    d = r.json()
                    proxy_status = d.get("status", 502)
                    if 200 <= proxy_status < 300:
                        # Proxy reachable AND target reachable through
                        # it — credit the proxy's health.
                        health.record_success(
                            intermediate.host, intermediate.port,
                            (time.time() - t0) * 1000,
                        )
                        logger.info(
                            "relay_via_proxy",
                            proxy=intermediate.node_id,
                            target=target_node_id,
                            proxy_status=proxy_status,
                            via_bridge=_is_bridge(intermediate),
                            hops_remaining=hops_remaining - 1,
                        )
                        return proxy_status, d.get("body"), {}
                    # 200 from proxy but target wasn't reachable through
                    # it — proxy is healthy, target via this route isn't.
                    # Record success on the proxy hop, fall through.
                    health.record_success(
                        intermediate.host, intermediate.port,
                        (time.time() - t0) * 1000,
                    )
                else:
                    health.record_failure(intermediate.host, intermediate.port)
            except Exception as proxy_err:
                health.record_failure(intermediate.host, intermediate.port)
                logger.debug("relay_proxy_failed",
                             proxy=intermediate.node_id, error=str(proxy_err))

    return 502, {"error": "all_relay_paths_failed"}, {}
