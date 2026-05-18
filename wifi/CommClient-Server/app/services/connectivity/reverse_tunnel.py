"""Reverse-tunnel client — pairs with ``Helen-Rendezvous``.

Wire protocol (JSON text frames over WebSocket — matches rendezvous main.py):

    Server→Rendezvous:
      {"type": "hello", "name": "<display name>"}           once after accept
      {"type": "response", "rid": "<id>", "status": 200,
       "headers": [[k, v], ...], "body_b64": "..."}
      {"type": "ping"}

    Rendezvous→Server:
      {"type": "welcome", "public_id": "...", "path_template": "/t/<id>/..."}
      {"type": "request", "rid": "<id>", "method": "GET",
       "path": "/api/health?x=1", "headers": [...], "body_b64": "..."}
      {"type": "pong"}

Design notes
------------
* Requests are dispatched to ``http://127.0.0.1:<local_port>`` via ``httpx``
  so the existing FastAPI app handles them unchanged. This keeps the tunnel
  logic oblivious to the server's endpoint catalogue.
* A semaphore caps concurrent tunneled requests so a traffic spike can't
  exhaust the event loop's task budget.
* Reconnect loop with exponential backoff — the tunnel comes back up
  automatically after rendezvous restarts or transient network drops.
* Fail-closed on missing token: if the rendezvous URL is set but no token,
  we refuse to start (would leak ``public_id`` and invite hijacking).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import random
from typing import Any

import httpx
import websockets

from app.core.logging import get_logger

logger = get_logger(__name__)


RECONNECT_BASE_DELAY = 2.0
RECONNECT_MAX_DELAY = 60.0
REQUEST_CONCURRENCY = 32
LOCAL_REQUEST_TIMEOUT = 15.0
KEEPALIVE_INTERVAL = 25.0

# Cap on concurrent proxied WebSocket sessions (socket.io connections via tunnel).
WS_PROXY_MAX_SESSIONS = 512


class ReverseTunnelClient:
    """Holds the outbound WebSocket to a Helen-Rendezvous instance.

    Usage (from the server's lifespan startup):

        client = ReverseTunnelClient(
            rendezvous_ws_url="ws://my-vps.example:9090/tunnel/register",
            token="shared-secret",
            local_base_url="http://127.0.0.1:3000",
            display_name="My Helen",
        )
        await client.start()
        ...
        await client.stop()

    ``public_id`` / ``path_template`` populate after the first successful
    handshake and are exposed via :meth:`status` for the admin dashboard.
    """

    def __init__(
        self,
        *,
        rendezvous_ws_url: str,
        token: str,
        local_base_url: str,
        display_name: str = "Helen Server",
    ) -> None:
        if not rendezvous_ws_url or not token:
            raise ValueError("rendezvous_ws_url and token are required")
        self._ws_url = rendezvous_ws_url.rstrip("/")
        if "token=" not in self._ws_url:
            sep = "&" if "?" in self._ws_url else "?"
            self._ws_url = f"{self._ws_url}{sep}token={token}"
        self._token = token
        self._local_base = local_base_url.rstrip("/")
        self._display_name = display_name

        self._task: asyncio.Task | None = None
        self._stop_evt = asyncio.Event()
        self._sem = asyncio.Semaphore(REQUEST_CONCURRENCY)
        self._http: httpx.AsyncClient | None = None
        # Parse the local base so we can build a matching ws:// URL for
        # the WebSocket proxy path.
        from urllib.parse import urlparse
        p = urlparse(self._local_base)
        self._local_ws_host = p.hostname or "127.0.0.1"
        self._local_ws_port = p.port or 3000
        # Active proxy sessions: wsid → asyncio.Task
        self._ws_sessions: dict[str, asyncio.Task] = {}
        # Inbound frame queues from the tunnel keyed by wsid.
        self._ws_inbound: dict[str, asyncio.Queue] = {}
        self._current_ws: websockets.WebSocketClientProtocol | None = None

        # Observable state — read by the orchestrator / admin API.
        self.public_id: str | None = None
        self.path_template: str | None = None
        self.connected_since: float | None = None
        self.last_error: str | None = None
        self.reconnect_count: int = 0

    # ── Lifecycle ──────────────────────────────────────
    async def start(self) -> None:
        if self._task is not None:
            return
        self._http = httpx.AsyncClient(
            base_url=self._local_base,
            timeout=LOCAL_REQUEST_TIMEOUT,
        )
        self._stop_evt.clear()
        self._task = asyncio.create_task(self._run(), name="helen-reverse-tunnel")
        logger.info("reverse_tunnel_starting",
                    url=self._safe_url(), local=self._local_base)

    async def stop(self) -> None:
        self._stop_evt.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._http is not None:
            await self._http.aclose()
            self._http = None
        logger.info("reverse_tunnel_stopped")

    def status(self) -> dict[str, Any]:
        return {
            "configured": True,
            "connected": self.connected_since is not None,
            "public_id": self.public_id,
            "path_template": self.path_template,
            "rendezvous_url": self._safe_url(),
            "reconnect_count": self.reconnect_count,
            "last_error": self.last_error,
            "connected_since": self.connected_since,
        }

    def _safe_url(self) -> str:
        # Strip the token from the logged/exposed URL to avoid leaking it in
        # admin dashboards or logs.
        if "token=" not in self._ws_url:
            return self._ws_url
        base, _, _query = self._ws_url.partition("?")
        return base

    # ── Main loop ──────────────────────────────────────
    async def _run(self) -> None:
        delay = RECONNECT_BASE_DELAY
        while not self._stop_evt.is_set():
            try:
                async with websockets.connect(
                    self._ws_url,
                    max_size=16 * 1024 * 1024,
                    ping_interval=KEEPALIVE_INTERVAL,
                    ping_timeout=KEEPALIVE_INTERVAL,
                ) as ws:
                    await self._handshake(ws)
                    delay = RECONNECT_BASE_DELAY       # reset backoff
                    await self._serve(ws)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
                self.connected_since = None
                self.public_id = None
                logger.warning("reverse_tunnel_disconnect", error=self.last_error)

            if self._stop_evt.is_set():
                break

            # Backoff with jitter to avoid stampede.
            jitter = random.uniform(0, 0.5)
            await asyncio.sleep(min(delay + jitter, RECONNECT_MAX_DELAY))
            delay = min(delay * 2, RECONNECT_MAX_DELAY)
            self.reconnect_count += 1

    async def _handshake(self, ws: websockets.WebSocketClientProtocol) -> None:
        await ws.send(json.dumps({"type": "hello", "name": self._display_name}))
        welcome_raw = await asyncio.wait_for(ws.recv(), timeout=10)
        welcome = json.loads(welcome_raw)
        if welcome.get("type") != "welcome":
            raise RuntimeError(f"unexpected handshake reply: {welcome!r}")
        self.public_id = str(welcome.get("public_id") or "")
        self.path_template = welcome.get("path_template")
        self.connected_since = asyncio.get_event_loop().time()
        self.last_error = None
        logger.info("reverse_tunnel_connected",
                    public_id=self.public_id, template=self.path_template)

    async def _serve(self, ws: websockets.WebSocketClientProtocol) -> None:
        tasks: set[asyncio.Task] = set()
        self._current_ws = ws
        try:
            async for raw in ws:
                try:
                    frame = json.loads(raw)
                except Exception:
                    continue
                ftype = frame.get("type")
                if ftype == "request":
                    task = asyncio.create_task(self._handle_request(ws, frame))
                    tasks.add(task)
                    task.add_done_callback(tasks.discard)
                elif ftype == "ws_open":
                    await self._handle_ws_open(ws, frame)
                elif ftype in ("ws_frame", "ws_close"):
                    wsid = str(frame.get("wsid") or "")
                    q = self._ws_inbound.get(wsid)
                    if q is not None:
                        with contextlib.suppress(asyncio.QueueFull):
                            q.put_nowait(frame)
                elif ftype == "pong":
                    continue
                # silently ignore unknown frames
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            # Tear down any lingering WS-proxy sessions; the rendezvous
            # will emit ws_close to external clients when our tunnel dies.
            for sid, task in list(self._ws_sessions.items()):
                task.cancel()
            self._ws_sessions.clear()
            self._ws_inbound.clear()
            self._current_ws = None

    async def _handle_ws_open(
        self,
        tunnel: websockets.WebSocketClientProtocol,
        frame: dict[str, Any],
    ) -> None:
        """An external client opened a WebSocket via the rendezvous. Bridge
        it to a local WebSocket connection on our own server."""
        wsid = str(frame.get("wsid") or "")
        if not wsid or wsid in self._ws_sessions:
            return
        if len(self._ws_sessions) >= WS_PROXY_MAX_SESSIONS:
            await tunnel.send(json.dumps({
                "type": "ws_close", "wsid": wsid,
                "code": 1013, "reason": "tunnel saturated",
            }))
            return
        path = str(frame.get("path") or "/")
        headers_raw = frame.get("headers") or []
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._ws_inbound[wsid] = q
        task = asyncio.create_task(
            self._run_ws_bridge(tunnel, wsid, path, headers_raw, q),
            name=f"helen-tunnel-ws-{wsid}",
        )
        self._ws_sessions[wsid] = task
        task.add_done_callback(lambda _t, s=wsid: self._cleanup_ws(s))

    def _cleanup_ws(self, wsid: str) -> None:
        self._ws_sessions.pop(wsid, None)
        self._ws_inbound.pop(wsid, None)

    async def _run_ws_bridge(
        self,
        tunnel: websockets.WebSocketClientProtocol,
        wsid: str,
        path: str,
        headers_raw: list[Any],
        inbound: asyncio.Queue,
    ) -> None:
        """Open a local WebSocket to our own server and pipe frames to/from
        the tunnel's external client."""
        local_ws_url = f"ws://{self._local_ws_host}:{self._local_ws_port}{path}"
        # Forward a conservative subset of headers — the ones that matter for
        # Socket.IO routing (Cookie, Authorization, Origin, User-Agent).
        forward_headers: dict[str, str] = {}
        for pair in headers_raw:
            if isinstance(pair, list) and len(pair) == 2:
                name = str(pair[0]).strip()
                if name.lower() in {
                    "cookie", "authorization", "origin", "user-agent",
                    "x-forwarded-for", "x-real-ip",
                }:
                    forward_headers[name] = str(pair[1])

        # `websockets` renamed `extra_headers` → `additional_headers` in v11+.
        # Try the new name first, fall back to the legacy one so both the
        # current package and older pinned environments work.
        connect_kwargs: dict[str, Any] = {
            "max_size": 16 * 1024 * 1024,
            "ping_interval": None,   # let app-layer protocol handle keepalive
        }
        if forward_headers:
            import inspect as _inspect
            sig = _inspect.signature(websockets.connect)
            if "additional_headers" in sig.parameters:
                connect_kwargs["additional_headers"] = list(forward_headers.items())
            elif "extra_headers" in sig.parameters:
                connect_kwargs["extra_headers"] = list(forward_headers.items())

        try:
            async with websockets.connect(local_ws_url, **connect_kwargs) as local_ws:
                async def up() -> None:
                    """External client → tunnel → us → local server."""
                    import base64 as _b64
                    while True:
                        frame = await inbound.get()
                        ft = frame.get("type")
                        if ft == "ws_close":
                            return
                        if ft != "ws_frame":
                            continue
                        kind = frame.get("kind")
                        if kind == "text":
                            await local_ws.send(frame.get("data") or "")
                        else:
                            data = frame.get("data_b64") or ""
                            await local_ws.send(
                                _b64.b64decode(data.encode("ascii")) if data else b"",
                            )

                async def down() -> None:
                    """Local server → us → tunnel → external client."""
                    import base64 as _b64
                    async for msg in local_ws:
                        if isinstance(msg, str):
                            frame = {"type": "ws_frame", "wsid": wsid,
                                     "kind": "text", "data": msg}
                        else:
                            frame = {"type": "ws_frame", "wsid": wsid,
                                     "kind": "binary",
                                     "data_b64": _b64.b64encode(msg).decode("ascii")}
                        await tunnel.send(json.dumps(frame))

                up_t = asyncio.create_task(up())
                down_t = asyncio.create_task(down())
                done, pending = await asyncio.wait(
                    {up_t, down_t}, return_when=asyncio.FIRST_COMPLETED,
                )
                for p in pending:
                    p.cancel()
        except Exception as e:
            logger.warning("reverse_tunnel_ws_bridge_error", wsid=wsid, error=str(e))
        finally:
            with contextlib.suppress(Exception):
                await tunnel.send(json.dumps({
                    "type": "ws_close", "wsid": wsid,
                    "code": 1000, "reason": "",
                }))

    async def _handle_request(
        self,
        ws: websockets.WebSocketClientProtocol,
        frame: dict[str, Any],
    ) -> None:
        rid = str(frame.get("rid") or "")
        if not rid:
            return
        async with self._sem:
            try:
                response = await self._dispatch_locally(frame)
            except Exception as e:
                logger.warning("reverse_tunnel_dispatch_error", rid=rid, error=str(e))
                response = {
                    "type": "response",
                    "rid": rid,
                    "status": 502,
                    "headers": [["content-type", "text/plain; charset=utf-8"]],
                    "body_b64": base64.b64encode(
                        f"tunnel dispatch failed: {e}".encode()
                    ).decode("ascii"),
                }
            try:
                await ws.send(json.dumps(response))
            except websockets.ConnectionClosed:
                return

    async def _dispatch_locally(self, frame: dict[str, Any]) -> dict[str, Any]:
        method = str(frame.get("method") or "GET").upper()
        path = str(frame.get("path") or "/")
        headers_raw = frame.get("headers") or []
        body_b64 = frame.get("body_b64") or ""
        body = base64.b64decode(body_b64.encode("ascii")) if body_b64 else b""

        # httpx wants a dict but lower-case duplicate headers (e.g. Cookie
        # pairs) are rare for Helen's API surface — flatten to last-wins.
        hdr: dict[str, str] = {}
        for pair in headers_raw:
            if isinstance(pair, list) and len(pair) == 2:
                hdr[str(pair[0])] = str(pair[1])

        assert self._http is not None
        resp = await self._http.request(method, path, headers=hdr, content=body)

        # Serialize response headers as a list of [k, v] pairs so duplicate
        # headers (Set-Cookie) survive round-trip through JSON.
        out_headers: list[list[str]] = [
            [k, v] for (k, v) in resp.headers.items()
        ]
        return {
            "type": "response",
            "rid": str(frame.get("rid")),
            "status": resp.status_code,
            "headers": out_headers,
            "body_b64": base64.b64encode(resp.content).decode("ascii"),
        }
