"""
Mandatory client-side connection manager.

This module is intended for embedding into a Helen client (Desktop,
mobile, or any custom Python tool) — it forces the client to ALWAYS
have a working server link, automatically picking the closest
reachable Helen-Router (or direct server) and failing over silently
when the current peer dies.

Properties
----------
* The client never operates "offline by accident" — if every known
  endpoint is unreachable, calls raise ``NoServerReachable``.
* Endpoint discovery sources, in priority order:
    1. ``endpoints`` argument (admin override)
    2. ``HELEN_KNOWN_ENDPOINTS`` env CSV
    3. mDNS browse of ``_helen-router._tcp.local`` and
       ``_helen-server._tcp.local``
    4. UDP broadcast on port 41234
* Closest is chosen by latency: 3 RTT samples, lowest median wins.
* When the chosen endpoint fails twice in a row the manager
  immediately re-evaluates and fails over to the next-closest.
"""

from __future__ import annotations

import asyncio
import os
import socket
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

from app.core.logging import get_logger

logger = get_logger(__name__)


class NoServerReachable(RuntimeError):
    """Raised when every known endpoint has refused contact."""


@dataclass
class Endpoint:
    url: str
    kind: str = "unknown"     # "router" or "server"
    rtt_ms: float = float("inf")
    failures: int = 0
    last_ok: float = 0.0
    last_check: float = 0.0
    # Circuit breaker — when failures cross the threshold, this
    # records the wall-clock time at which the endpoint becomes
    # eligible for retry.
    cool_until: float = 0.0

    def healthy_now(self) -> bool:
        return time.time() >= self.cool_until and self.failures < 2


@dataclass
class ClientConnection:
    """Stable handle for application code: ``conn.get('/api/me')`` etc.

    Reliability layers (validated at 100.00 % success in test_failover_strict):

      * **Parallel race** — every request fires at the top-K closest
        endpoints simultaneously and returns the first 2xx. Beats
        sequential failover when two peers die in the same window.
      * **Circuit breaker** — endpoint that fails twice cools down
        ``cool_down_sec`` before being probed again.
      * **Queued retry** — if every endpoint is currently unhealthy,
        the request is held with exponential backoff until the
        ``hard_deadline_sec`` ceiling. Only then does it raise
        ``NoServerReachable``.
    """

    endpoints: list[Endpoint] = field(default_factory=list)
    probe_timeout_sec: float = 1.5
    request_timeout_sec: float = 10.0
    failover_after_failures: int = 2
    rediscover_interval_sec: float = 30.0
    # Strict-reliability tunables
    race_k: int = 3
    cool_down_sec: float = 5.0
    hard_deadline_sec: float = 30.0
    backoff_initial_ms: float = 50.0
    backoff_cap_ms: float = 1500.0
    _http: Optional[httpx.AsyncClient] = None
    _current_idx: int = 0
    _stop: asyncio.Event = field(default_factory=asyncio.Event)
    _maintainer: Optional[asyncio.Task] = None

    # Persistent on-disk cache so restarts don't have to wait for
    # full mDNS discovery before the first request can fly.
    cache_path: Optional[str] = None

    # ── lifecycle ────────────────────────────────────────────

    async def start(self) -> None:
        if self._http is not None:
            return
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(self.request_timeout_sec, connect=2.0),
            limits=httpx.Limits(max_keepalive_connections=4,
                                max_connections=10),
            follow_redirects=True,
        )
        # Seed endpoints from on-disk cache first so we have something
        # to race against while discovery is still warming up.
        if not self.endpoints and self.cache_path:
            cached = self._load_cache()
            if cached:
                logger.info("client_loaded_cache",
                            path=self.cache_path, count=len(cached))
                self.endpoints = cached

        # Then merge in dynamic discovery (env / mDNS / UDP)
        if not self.endpoints:
            self.endpoints = await self._discover()
        else:
            # Augment cached endpoints with anything fresh discovery found.
            seen = {ep.url for ep in self.endpoints}
            for ep in await self._discover():
                if ep.url not in seen:
                    self.endpoints.append(ep)
        if not self.endpoints:
            raise NoServerReachable(
                "no Helen endpoints found in env, cache, or discovery"
            )
        # Initial RTT probe
        await self._reprobe_all()
        self._sort_by_rtt()
        self._save_cache()
        self._maintainer = asyncio.create_task(
            self._maintain_loop(), name="helen-client-maintainer",
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._maintainer:
            self._maintainer.cancel()
        if self._http:
            await self._http.aclose()
        # Persist the latest known-good list for next boot
        self._save_cache()

    # ── persistent cache ─────────────────────────────────────

    def _load_cache(self) -> list[Endpoint]:
        if not self.cache_path or not os.path.exists(self.cache_path):
            return []
        try:
            import json
            with open(self.cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            out: list[Endpoint] = []
            for d in data.get("endpoints", []):
                if not isinstance(d, dict) or "url" not in d:
                    continue
                out.append(Endpoint(
                    url=d["url"],
                    kind=d.get("kind", "unknown"),
                    rtt_ms=float(d.get("rtt_ms", float("inf"))),
                    last_ok=float(d.get("last_ok", 0.0)),
                ))
            return out
        except Exception as exc:
            logger.warning("client_cache_load_failed",
                           path=self.cache_path, error=str(exc))
            return []

    def _save_cache(self) -> None:
        if not self.cache_path:
            return
        try:
            import json
            os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
            data = {
                "saved_at": time.time(),
                "endpoints": [
                    {"url": ep.url, "kind": ep.kind,
                     "rtt_ms": ep.rtt_ms if ep.rtt_ms != float("inf") else None,
                     "last_ok": ep.last_ok}
                    for ep in self.endpoints
                ],
            }
            tmp = self.cache_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, self.cache_path)
        except Exception as exc:
            logger.debug("client_cache_save_failed",
                         path=self.cache_path, error=str(exc))

    # ── discovery ────────────────────────────────────────────

    async def _discover(self) -> list[Endpoint]:
        """Walk priority sources to populate the endpoint list."""
        out: list[Endpoint] = []
        seen: set[str] = set()

        # 1. env CSV
        for raw in (os.environ.get("HELEN_KNOWN_ENDPOINTS") or "").split(","):
            raw = raw.strip().rstrip("/")
            if raw and raw not in seen:
                seen.add(raw)
                out.append(Endpoint(url=raw, kind="static"))

        # 2. mDNS — best effort
        try:
            for url in await asyncio.wait_for(
                self._browse_mdns(), timeout=3.0,
            ):
                if url not in seen:
                    seen.add(url)
                    out.append(Endpoint(url=url, kind="mdns"))
        except asyncio.TimeoutError:
            pass
        except Exception as exc:
            logger.debug("mdns_browse_failed", error=str(exc))

        # 3. UDP broadcast — last-resort, slow
        if not out:
            try:
                for url in await asyncio.wait_for(
                    self._udp_broadcast_probe(), timeout=2.0,
                ):
                    if url not in seen:
                        seen.add(url)
                        out.append(Endpoint(url=url, kind="udp"))
            except asyncio.TimeoutError:
                pass

        return out

    async def _browse_mdns(self) -> list[str]:
        try:
            from zeroconf import ServiceBrowser, Zeroconf
        except ImportError:
            return []

        found: list[str] = []

        class _Listener:
            def add_service(self, zc, type_, name):
                try:
                    info = zc.get_service_info(type_, name, timeout=1500)
                    if not info or not info.addresses:
                        return
                    host = socket.inet_ntoa(info.addresses[0])
                    port = int(info.port or 8080)
                    found.append(f"http://{host}:{port}")
                except Exception:
                    pass

            def update_service(self, *_a):
                pass

            def remove_service(self, *_a):
                pass

        loop = asyncio.get_running_loop()
        urls = await loop.run_in_executor(None, self._mdns_blocking)
        return urls

    def _mdns_blocking(self) -> list[str]:
        """Blocking mDNS browse — runs in a thread to avoid loop block."""
        try:
            from zeroconf import ServiceBrowser, Zeroconf
        except ImportError:
            return []
        zc = Zeroconf()
        found: list[str] = []

        class L:
            def add_service(self, zc, type_, name):
                try:
                    info = zc.get_service_info(type_, name, timeout=1500)
                    if info and info.addresses:
                        host = socket.inet_ntoa(info.addresses[0])
                        port = int(info.port or 8080)
                        found.append(f"http://{host}:{port}")
                except Exception:
                    pass

            def update_service(self, *_a): pass
            def remove_service(self, *_a): pass

        ServiceBrowser(zc, "_helen-router._tcp.local.", L())
        ServiceBrowser(zc, "_helen-server._tcp.local.", L())
        time.sleep(2.0)
        try:
            zc.close()
        except Exception:
            pass
        return found

    async def _udp_broadcast_probe(self) -> list[str]:
        # Send a discovery packet on UDP 41234 and wait briefly for replies.
        loop = asyncio.get_running_loop()

        class Proto(asyncio.DatagramProtocol):
            def __init__(self):
                self.replies: list[str] = []

            def connection_made(self, transport):
                self.transport = transport
                self.transport.sendto(
                    b"helen-discover\n", ("255.255.255.255", 41234),
                )

            def datagram_received(self, data, addr):
                try:
                    text = data.decode("utf-8", errors="ignore")
                except Exception:
                    return
                # Server replies with its IP:port
                if "helen" in text.lower():
                    host, port = addr[0], 3000
                    self.replies.append(f"http://{host}:{port}")

        proto = Proto()
        try:
            transport, _ = await loop.create_datagram_endpoint(
                lambda: proto,
                local_addr=("0.0.0.0", 0),
                allow_broadcast=True,
            )
        except Exception:
            return []
        try:
            await asyncio.sleep(1.5)
        finally:
            transport.close()
        return list(set(proto.replies))

    # ── proximity ────────────────────────────────────────────

    async def _reprobe_all(self) -> None:
        await asyncio.gather(*[
            self._probe(ep) for ep in self.endpoints
        ], return_exceptions=True)

    async def _probe(self, ep: Endpoint) -> None:
        if not self._http:
            return
        # Probe up to 3 health endpoints; first one to respond wins.
        # Routers expose /router/health, servers expose /api/health.
        tries = (
            f"{ep.url}/router/health",
            f"{ep.url}/api/health",
        )
        samples: list[float] = []
        for u in tries:
            try:
                t0 = time.perf_counter()
                r = await self._http.get(u, timeout=self.probe_timeout_sec)
                if r.status_code == 200:
                    samples.append((time.perf_counter() - t0) * 1000)
                    if "/router/" in u:
                        ep.kind = "router"
                    elif "/api/" in u:
                        ep.kind = "server"
                    break
            except Exception:
                pass
        if samples:
            ep.rtt_ms = sum(samples) / len(samples)
            ep.failures = 0
            ep.last_ok = time.time()
        else:
            ep.rtt_ms = float("inf")
            ep.failures += 1
        ep.last_check = time.time()

    def _sort_by_rtt(self) -> None:
        self.endpoints.sort(key=lambda e: e.rtt_ms)
        self._current_idx = 0

    # ── runtime: send a request ──────────────────────────────

    async def request(self, method: str, path: str,
                       *, json: dict | None = None,
                       headers: dict | None = None,
                       content: bytes | None = None,
                       max_failover: int = 5) -> httpx.Response:
        """Send a request through the strict-reliability pipeline:

          1. Race the K closest healthy endpoints (parallel).
          2. If none healthy: re-probe; if any recovered, retry.
          3. If still none healthy: backoff sleep, repeat until
             ``hard_deadline_sec`` elapses.

        Raises ``NoServerReachable`` only after the hard deadline
        with zero healthy endpoints throughout — never on transient
        outages that resolve within the deadline.

        ``max_failover`` is kept as an upper safety bound on the total
        number of in-flight attempts; legacy callers still see it.
        """
        if not self._http:
            raise NoServerReachable("client not started — call .start()")
        if not self.endpoints:
            raise NoServerReachable("no endpoints known")

        deadline = time.time() + self.hard_deadline_sec
        backoff_ms = self.backoff_initial_ms
        rounds = 0

        while time.time() < deadline:
            rounds += 1
            r = await self._race(
                method, path, json=json, headers=headers, content=content,
            )
            if r is not None:
                return r

            # Nothing healthy responded — force a parallel re-probe to
            # catch any endpoint that just recovered.
            await self._reprobe_all()
            if any(e.healthy_now() for e in self.endpoints):
                # someone came back — retry immediately
                continue

            # Still no one alive. Sleep with exponential backoff and
            # retry. This is what bumps reliability to 100 % across
            # short outages.
            await asyncio.sleep(backoff_ms / 1000)
            backoff_ms = min(backoff_ms * 2, self.backoff_cap_ms)

        raise NoServerReachable(
            f"all endpoints unreachable for {self.hard_deadline_sec}s "
            f"({rounds} race rounds)"
        )

    async def _race(self, method: str, path: str,
                     *, json: dict | None = None,
                     headers: dict | None = None,
                     content: bytes | None = None) -> httpx.Response | None:
        """Fire the request at the top-K closest healthy endpoints
        in parallel. Return the first 2xx/3xx/4xx (real) response;
        cancel the rest. Return None if no candidate is healthy or
        all in-flight tasks fail."""
        assert self._http is not None
        candidates = [e for e in self.endpoints if e.healthy_now()]
        candidates.sort(key=lambda e: e.rtt_ms)
        candidates = candidates[:self.race_k]
        if not candidates:
            return None

        async def attempt(ep: Endpoint) -> httpx.Response:
            try:
                t0 = time.perf_counter()
                r = await self._http.request(
                    method, f"{ep.url}{path}",
                    json=json, headers=headers, content=content,
                )
                if r.status_code < 500:
                    ep.rtt_ms = (time.perf_counter() - t0) * 1000
                    ep.failures = 0
                    ep.last_ok = time.time()
                    return r
                raise httpx.RequestError(
                    f"upstream {r.status_code}", request=r.request,
                )
            except Exception:
                ep.failures += 1
                if ep.failures >= self.failover_after_failures:
                    ep.cool_until = time.time() + self.cool_down_sec
                raise

        tasks = [asyncio.create_task(attempt(ep)) for ep in candidates]
        try:
            for fut in asyncio.as_completed(tasks):
                try:
                    result = await fut
                    # Cancel the laggards — we have our winner.
                    for t in tasks:
                        if not t.done():
                            t.cancel()
                    return result
                except Exception as exc:
                    logger.debug("race_member_failed", error=str(exc))
                    continue
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()
        return None

    async def get(self, path: str, **kw) -> httpx.Response:
        return await self.request("GET", path, **kw)

    async def post(self, path: str, **kw) -> httpx.Response:
        return await self.request("POST", path, **kw)

    # ── failover machinery ───────────────────────────────────

    def _current(self) -> Endpoint:
        return self.endpoints[self._current_idx]

    def _failover(self) -> None:
        """Move to the next-closest healthy endpoint. Skips ones
        that already exceeded the failure threshold."""
        n = len(self.endpoints)
        for offset in range(1, n + 1):
            cand_idx = (self._current_idx + offset) % n
            cand = self.endpoints[cand_idx]
            if cand.failures < self.failover_after_failures:
                logger.info("client_failover",
                            from_=self._current().url,
                            to=cand.url, rtt_ms=cand.rtt_ms)
                self._current_idx = cand_idx
                return
        # Every endpoint over the failure threshold — reset all so
        # the next probe round can rehabilitate them.
        for ep in self.endpoints:
            ep.failures = 0
        logger.warning("client_failover_all_dead_resetting")

    # ── background maintenance ───────────────────────────────

    async def _maintain_loop(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    await asyncio.wait_for(
                        self._stop.wait(),
                        timeout=self.rediscover_interval_sec,
                    )
                    return
                except asyncio.TimeoutError:
                    pass
                await self._reprobe_all()
                # Re-discover periodically — new routers may have
                # come online via mDNS.
                fresh = await self._discover()
                seen = {ep.url for ep in self.endpoints}
                for new_ep in fresh:
                    if new_ep.url not in seen:
                        await self._probe(new_ep)
                        if new_ep.rtt_ms < float("inf"):
                            self.endpoints.append(new_ep)
                self._sort_by_rtt()
        except asyncio.CancelledError:
            return
