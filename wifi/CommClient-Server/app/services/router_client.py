"""
Helen-Router auto-registration client (multi-router capable).

A Helen-Server can be reachable through one or many routers
simultaneously. This module keeps the server registered with each of
them in parallel — every router learns of us within one heartbeat
cycle, and a stale router (one that has stopped heartbeat-replying)
is dropped automatically.

Discovery sources (any combination):

  1. ``HELEN_ROUTER_URL``   — single router URL (legacy/simple)
  2. ``HELEN_ROUTER_URLS``  — CSV of router URLs (multi-router)
  3. mDNS browse of ``_helen-router._tcp.local`` — auto-discovery

For each source the same shared token (``HELEN_ROUTER_TOKEN``) is used,
unless a per-router override is provided via:

  ``HELEN_ROUTER_TOKENS``   — CSV of ``url=token`` pairs (rare; used
                              when each router has its own secret).

Why "many routers at once"
--------------------------
  - High availability: any single router crash doesn't take the server
    offline; clients can reach the others.
  - Geographic redundancy: one router per branch / VLAN / floor.
  - Rolling upgrades: replace one router at a time without downtime.
  - Fault isolation: an attacker who compromises one router cannot
    de-register the server from the others.

This module is best-effort throughout. Every routine logs but never
raises; the server keeps serving traffic regardless of router state.
"""

from __future__ import annotations

import asyncio
import os
import socket
from typing import Any

import httpx

from app.core.logging import get_logger

logger = get_logger(__name__)


_HEARTBEAT_INTERVAL_SEC = 30.0
_TIMEOUT_SEC = 5.0
_MDNS_REFRESH_SEC = 60.0


# ── Helpers ─────────────────────────────────────────────────────────


def _server_id() -> str:
    """Stable identifier for this server."""
    try:
        from app.services.discovery_service import get_server_id
        sid = get_server_id() or ""
        if sid:
            return sid
    except Exception:
        pass
    return socket.gethostname() or "helen-server"


def _self_url() -> str | None:
    """Best-effort guess at the URL routers should use to reach us."""
    explicit = os.environ.get("HELEN_ROUTER_SELF_URL")
    if explicit:
        return explicit.rstrip("/")

    port = os.environ.get("PORT", "3000")
    bind_ip = os.environ.get("HELEN_BIND_IP")
    if bind_ip:
        return f"http://{bind_ip}:{port}"

    try:
        import psutil
        for ifname, addrs in psutil.net_if_addrs().items():
            for a in addrs:
                if a.family != socket.AF_INET:
                    continue
                ip = a.address
                if (ip and ip != "127.0.0.1"
                        and not ip.startswith("169.254.")):
                    return f"http://{ip}:{port}"
    except Exception:
        pass

    return f"http://127.0.0.1:{port}"


def _parse_router_token_overrides(raw: str) -> dict[str, str]:
    """Parse ``HELEN_ROUTER_TOKENS=url1=tok1,url2=tok2`` into a dict."""
    out: dict[str, str] = {}
    if not raw:
        return out
    for part in raw.split(","):
        part = part.strip()
        if "=" not in part:
            continue
        url, tok = part.split("=", 1)
        out[url.strip().rstrip("/")] = tok.strip()
    return out


# ── Per-router client ───────────────────────────────────────────────


class _SingleRouterClient:
    """Registration + heartbeat against ONE router."""

    def __init__(
        self,
        router_url: str,
        token: str,
        server_id: str,
        self_url: str,
        capabilities: list[str],
    ) -> None:
        self.router_url = router_url.rstrip("/")
        self.token = token
        self.server_id = server_id
        self.self_url = self_url
        self.capabilities = capabilities
        self._http: httpx.AsyncClient | None = None
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(_TIMEOUT_SEC, connect=2.0),
        )
        self._stop.clear()
        self._task = asyncio.create_task(
            self._run(),
            name=f"router-registration:{self.router_url}",
        )
        logger.info("router_registration_started",
                    router=self.router_url,
                    server_id=self.server_id, self_url=self.self_url)

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=3.0)
            except (asyncio.TimeoutError, Exception):
                self._task.cancel()
            self._task = None
        if self._http is not None:
            try:
                await self._unregister()
            except Exception:
                pass
            await self._http.aclose()
            self._http = None
        logger.info("router_registration_stopped",
                    router=self.router_url)

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    async def _register(self) -> bool:
        assert self._http is not None
        try:
            r = await self._http.post(
                f"{self.router_url}/router/register",
                headers=self._headers,
                json={
                    "server_id": self.server_id,
                    "url": self.self_url,
                    "capabilities": self.capabilities,
                },
            )
            if r.status_code == 200:
                logger.info("router_registered",
                            router=self.router_url,
                            server_id=self.server_id)
                return True
            logger.warning("router_register_failed",
                           router=self.router_url,
                           status=r.status_code, body=r.text[:200])
            return False
        except Exception as exc:
            logger.debug("router_register_error",
                         router=self.router_url, error=str(exc))
            return False

    async def _heartbeat(self) -> bool:
        assert self._http is not None
        try:
            r = await self._http.post(
                f"{self.router_url}/router/heartbeat/{self.server_id}",
                headers=self._headers,
            )
            if r.status_code == 200:
                return True
            if r.status_code == 404:
                return await self._register()
            return False
        except Exception:
            return False

    async def _unregister(self) -> None:
        assert self._http is not None
        try:
            await self._http.delete(
                f"{self.router_url}/router/register/{self.server_id}",
                headers=self._headers,
            )
        except Exception:
            pass

    async def _run(self) -> None:
        # Initial register with bounded backoff
        for attempt in range(5):
            if self._stop.is_set():
                return
            if await self._register():
                break
            await asyncio.sleep(min(2 ** attempt, 30.0))

        while not self._stop.is_set():
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=_HEARTBEAT_INTERVAL_SEC,
                )
                return
            except asyncio.TimeoutError:
                pass
            await self._heartbeat()


# ── Multi-router manager ────────────────────────────────────────────


class RouterRegistrationManager:
    """Owns a set of ``_SingleRouterClient`` instances and keeps it in
    sync with the configured / auto-discovered router list.

    Sync sources are merged on every refresh:

      * static URLs from ``HELEN_ROUTER_URL`` / ``HELEN_ROUTER_URLS``
      * mDNS browse results (if zeroconf available + not disabled)
    """

    def __init__(
        self,
        default_token: str,
        token_overrides: dict[str, str] | None = None,
    ) -> None:
        self.default_token = default_token
        self.token_overrides = token_overrides or {}
        self._server_id = _server_id()
        self._self_url = _self_url() or "http://127.0.0.1:3000"
        self._clients: dict[str, _SingleRouterClient] = {}
        self._mdns_zc: Any = None
        self._mdns_browser: Any = None
        self._mdns_routers: set[str] = set()
        self._refresh_task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    # ── public ──────────────────────────────────────────────────

    async def start(self, static_urls: list[str]) -> None:
        if self._refresh_task is not None:
            return
        for url in static_urls:
            await self._ensure_client(url, source="static")

        # Optional mDNS browse — disabled with HELEN_ROUTER_NO_MDNS=1
        if os.environ.get("HELEN_ROUTER_NO_MDNS", "").lower() not in (
                "1", "true", "yes"):
            try:
                self._start_mdns_browser()
            except Exception as exc:
                logger.warning("router_mdns_browse_failed",
                               error=str(exc))

        self._stop.clear()
        self._refresh_task = asyncio.create_task(
            self._refresh_loop(), name="router-multi-refresh"
        )
        logger.info("router_multi_started",
                    static_count=len(static_urls),
                    mdns=self._mdns_zc is not None)

    async def stop(self) -> None:
        self._stop.set()
        if self._refresh_task is not None:
            try:
                await asyncio.wait_for(self._refresh_task, timeout=2.0)
            except (asyncio.TimeoutError, Exception):
                self._refresh_task.cancel()
            self._refresh_task = None
        if self._mdns_zc is not None:
            try:
                self._mdns_zc.close()
            except Exception:
                pass
            self._mdns_zc = None
            self._mdns_browser = None
        await asyncio.gather(
            *(c.stop() for c in self._clients.values()),
            return_exceptions=True,
        )
        self._clients.clear()
        logger.info("router_multi_stopped")

    def known_routers(self) -> list[str]:
        return sorted(self._clients.keys())

    # ── internals ───────────────────────────────────────────────

    def _token_for(self, url: str) -> str:
        return self.token_overrides.get(url.rstrip("/"),
                                        self.default_token)

    async def _ensure_client(self, url: str, source: str) -> None:
        url = url.rstrip("/")
        if url in self._clients:
            return
        token = self._token_for(url)
        if not token:
            logger.warning("router_no_token_skipped",
                           router=url, source=source)
            return
        client = _SingleRouterClient(
            router_url=url,
            token=token,
            server_id=self._server_id,
            self_url=self._self_url,
            capabilities=["rest", "socketio", "webrtc", "vault"],
        )
        await client.start()
        self._clients[url] = client
        logger.info("router_added", router=url, source=source)

    async def _drop_client(self, url: str) -> None:
        url = url.rstrip("/")
        client = self._clients.pop(url, None)
        if client is not None:
            await client.stop()
            logger.info("router_dropped", router=url)

    def _start_mdns_browser(self) -> None:
        try:
            from zeroconf import ServiceBrowser, Zeroconf, ServiceListener
        except ImportError:
            logger.info("router_mdns_browse_skipped_no_zeroconf")
            return

        manager = self

        class _Listener(ServiceListener):
            def add_service(self, zc, type_, name):
                self._handle(zc, type_, name)

            def update_service(self, zc, type_, name):
                self._handle(zc, type_, name)

            def remove_service(self, zc, type_, name):
                pass  # left to _refresh_loop to age out

            def _handle(self, zc, type_, name):
                try:
                    info = zc.get_service_info(type_, name, timeout=2000)
                    if not info:
                        return
                    props = {}
                    for k, v in (info.properties or {}).items():
                        try:
                            props[k.decode() if isinstance(k, bytes) else k] = (
                                v.decode() if isinstance(v, bytes) else v
                            )
                        except Exception:
                            continue
                    host = (
                        socket.inet_ntoa(info.addresses[0])
                        if info.addresses
                        else (info.server or "").rstrip(".")
                    )
                    port = int(info.port or 8080)
                    url = f"http://{host}:{port}"
                    if url not in manager._mdns_routers:
                        manager._mdns_routers.add(url)
                        logger.info("router_mdns_discovered", url=url)
                except Exception:
                    pass

        # Build Zeroconf in a thread to dodge EventLoopBlocked on Windows
        import threading
        box: dict = {}

        def _ctor():
            try:
                zc = Zeroconf()
                box["zc"] = zc
                box["browser"] = ServiceBrowser(
                    zc, "_helen-router._tcp.local.", _Listener()
                )
            except Exception as exc:
                box["err"] = exc

        t = threading.Thread(target=_ctor, daemon=True)
        t.start()
        t.join(timeout=5.0)
        if "err" in box or "zc" not in box:
            return
        self._mdns_zc = box["zc"]
        self._mdns_browser = box["browser"]

    async def _refresh_loop(self) -> None:
        """Periodically reconcile the live client set with whatever
        sources currently see."""
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(),
                                       timeout=_MDNS_REFRESH_SEC)
                return
            except asyncio.TimeoutError:
                pass

            # Add any newly seen mDNS routers
            for url in list(self._mdns_routers):
                if url not in self._clients:
                    await self._ensure_client(url, source="mdns")


# ── Module-level state ──────────────────────────────────────────────


_MANAGER: RouterRegistrationManager | None = None


def get_router_manager() -> RouterRegistrationManager | None:
    return _MANAGER


async def maybe_start_router_client() -> None:
    """Read env, build a multi-router manager, start it.
    No-op if no router URL is configured AND mDNS browse is disabled."""
    global _MANAGER
    if _MANAGER is not None:
        return

    default_token = os.environ.get("HELEN_ROUTER_TOKEN", "").strip()
    token_overrides = _parse_router_token_overrides(
        os.environ.get("HELEN_ROUTER_TOKENS", ""),
    )

    static_urls: list[str] = []
    one = os.environ.get("HELEN_ROUTER_URL", "").strip()
    if one:
        static_urls.append(one)
    many = os.environ.get("HELEN_ROUTER_URLS", "").strip()
    if many:
        for u in many.split(","):
            u = u.strip()
            if u and u not in static_urls:
                static_urls.append(u)

    mdns_enabled = os.environ.get(
        "HELEN_ROUTER_NO_MDNS", "").lower() not in ("1", "true", "yes")

    if not static_urls and not mdns_enabled:
        return  # nothing to do

    if not default_token and not token_overrides:
        logger.warning(
            "router_url_set_but_no_token",
            detail="At least one router URL or mDNS browse is enabled, "
                   "but neither HELEN_ROUTER_TOKEN nor HELEN_ROUTER_TOKENS "
                   "is set; skipping auto-registration",
        )
        return

    _MANAGER = RouterRegistrationManager(
        default_token=default_token,
        token_overrides=token_overrides,
    )
    await _MANAGER.start(static_urls)


async def stop_router_client() -> None:
    global _MANAGER
    if _MANAGER is not None:
        await _MANAGER.stop()
        _MANAGER = None


# Backwards-compat alias for any callers that imported the singular name
def get_router_client() -> RouterRegistrationManager | None:
    return _MANAGER
