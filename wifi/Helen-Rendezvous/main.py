"""
Helen-Rendezvous — public-internet coordinator for Helen servers that sit
behind NAT / firewalls.

Runs on any VPS with a public IP. Exposes:

  WS  /tunnel/register                 Helen-Server opens an outbound WebSocket
                                       and keeps it alive; rendezvous holds the
                                       other end as a reverse-tunnel backhaul.

  ANY /t/<public_id>/<path>            External clients hit this URL; the
                                       rendezvous frames the request, ships it
                                       down the backhaul WebSocket, awaits the
                                       server's response, replays it to the
                                       external client. This is the
                                       reverse-tunnel dataplane.

  POST /signal/register                Helen-Server posts its observed public
                                       UDP endpoint (for hole-punch use).
  GET  /signal/lookup/<public_id>      Peer looks up the stored endpoint to
                                       start hole-punch exchange.

  TCP  port 9101  /  port 9102         Blind byte-level relay:
                                         :9101 receives `REGISTER <pid>\\n` from
                                            the Helen-Server (kept open).
                                         :9102 receives `LOOKUP <pid>\\n` from
                                            an external client.
                                       The two sockets are then joined.

Security
--------
This reference rendezvous uses a shared bootstrap token (`?token=...` query
param on /tunnel/register and `Authorization: Bearer ...` on /signal/*).
The token is loaded from the `HELEN_RENDEZVOUS_TOKEN` env var at startup. No
token = the endpoints reject all requests — fail closed.

Each tunnel keeps a short random `public_id` that must appear in the path
clients use to reach that server. The id is unguessable enough to serve as a
capability token for basic-case use; deploy behind HTTPS + rotate tokens in
production.

Scope note
----------
This file is the *reference* rendezvous. It is deliberately single-process,
in-memory, and stdlib-plus-FastAPI only. Horizontal scale (multi-instance,
Redis-backed registry) is left for a production deployment.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
import socket
import time
import uuid

# ── Cluster / storage extensions ──────────────────────────
# These imports never fail: the storage / cluster packages fall back to the
# in-memory backend if Redis / structlog aren't available, so existing
# single-instance deployments keep working with zero config changes.
try:
    from storage.factory import build_backend
    from storage.memory_backend import MemoryBackend
    from cluster.instance_registry import InstanceRegistry
    from cluster.affinity import SessionAffinity
    from cluster.cross_instance_relay import CrossInstanceRelay
    _CLUSTER_AVAILABLE = True
except Exception as _cluster_exc:  # pragma: no cover
    build_backend = None  # type: ignore[assignment]
    MemoryBackend = None  # type: ignore[assignment, misc]
    InstanceRegistry = None  # type: ignore[assignment, misc]
    SessionAffinity = None  # type: ignore[assignment, misc]
    CrossInstanceRelay = None  # type: ignore[assignment, misc]
    _CLUSTER_AVAILABLE = False


# Plain stdlib logger — keeps the rendezvous service free of structlog
# (one less dependency on a small box). Operators are expected to scrape
# the lines via journalctl + grep.
class _StructLikeLogger:
    """Tiny adapter so we can use the structlog-style kwargs everywhere."""
    def __init__(self, name: str = "helen-rendezvous") -> None:
        self._inner = logging.getLogger(name)
        if not self._inner.handlers:
            h = logging.StreamHandler()
            h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            self._inner.addHandler(h)
            self._inner.setLevel(logging.INFO)

    def _fmt(self, event: str, **kw: Any) -> str:
        if not kw:
            return event
        kvs = " ".join(f"{k}={v!r}" for k, v in kw.items())
        return f"{event} {kvs}"

    def info(self, event: str, **kw: Any) -> None:    self._inner.info(self._fmt(event, **kw))
    def warning(self, event: str, **kw: Any) -> None: self._inner.warning(self._fmt(event, **kw))
    def error(self, event: str, **kw: Any) -> None:   self._inner.error(self._fmt(event, **kw))

logger = _StructLikeLogger()
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response
from starlette.datastructures import Headers


# ── Configuration ─────────────────────────────────────────


# Tokens that the installer may have written when its 3-tier RNG fell
# all the way through. Refuse to boot with these — every install must
# end up with its own per-host token, never a known string.
_WEAK_TOKENS = frozenset({
    "0a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f9",
    "REPLACE_ME_BEFORE_RUNNING_HELEN_RENDEZVOUS_64_chars_long_xxxxxxxxxx",
    "change-me", "changeme", "secret",
})


def _load_token() -> str | None:
    """Shared bootstrap token — must match on server + rendezvous side.

    Absent = fail closed. Set via ``HELEN_RENDEZVOUS_TOKEN`` or a
    `.token` file sitting next to this module. Known-weak/installer-
    placeholder values raise so the deployment refuses to start
    rather than silently running with a guessable token.
    """
    tok = os.environ.get("HELEN_RENDEZVOUS_TOKEN")
    if not tok:
        here = os.path.dirname(os.path.abspath(__file__))
        tf = os.path.join(here, ".token")
        if os.path.exists(tf):
            try:
                with open(tf, encoding="utf-8") as f:
                    tok = f.read().strip() or None
            except OSError:
                tok = None
    if not tok:
        return None
    tok = tok.strip()
    if tok in _WEAK_TOKENS:
        raise RuntimeError(
            "HELEN_RENDEZVOUS_TOKEN is a known-weak / installer-placeholder "
            "value. Refusing to start. Edit the .env file (or the "
            "HELEN_RENDEZVOUS_TOKEN env var) and supply a fresh hex "
            "string from `openssl rand -hex 32`."
        )
    return tok


BOOTSTRAP_TOKEN = _load_token()

# Tunnel reply timeout — how long the rendezvous waits for the Helen-Server
# to answer a proxied HTTP request before giving up and 504-ing the client.
TUNNEL_REQUEST_TIMEOUT_SEC = 20.0

# Max inflight tunneled requests per backhaul — guards against a slow/dead
# server buffering forever while the rendezvous keeps accepting work.
TUNNEL_MAX_INFLIGHT = 64

# Relay TCP ports — operator can override via env.
RELAY_BACKEND_PORT = int(os.environ.get("HELEN_RELAY_BACKEND_PORT", "9101"))
RELAY_FRONTEND_PORT = int(os.environ.get("HELEN_RELAY_FRONTEND_PORT", "9102"))


# ── In-memory registries ──────────────────────────────────


@dataclass
class TunnelEntry:
    """One alive reverse-tunnel — the WebSocket stays open until the Helen
    server disconnects or a peer times out."""

    public_id: str
    name: str
    websocket: WebSocket
    connected_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    # HTTP-style request/response pairing (rid → Future[response frame])
    inflight: dict[str, asyncio.Future] = field(default_factory=dict)
    # WebSocket-proxy sessions (wsid → queue of inbound frames from server)
    ws_sessions: dict[str, asyncio.Queue] = field(default_factory=dict)
    _send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def send_json(self, obj: Any) -> None:
        async with self._send_lock:
            await self.websocket.send_text(json.dumps(obj))


@dataclass
class SignalEntry:
    public_id: str
    udp_endpoint: str       # "ip:port"
    meta: dict[str, Any] = field(default_factory=dict)
    updated_at: float = field(default_factory=time.time)


# Global registries — simple dicts guarded by coarse async locks.
# These continue to hold the *local* WebSocket-backed TunnelEntry objects, which
# cannot be shared across processes (they wrap live socket FDs). When cluster
# mode is enabled, the *shared* index lives in Redis (via `backend`) and points
# at the owning instance_id; the local dict here only ever holds entries this
# process is currently terminating.
tunnels: dict[str, TunnelEntry] = {}
signals: dict[str, SignalEntry] = {}
_registry_lock = asyncio.Lock()


# ── Shared cluster state ──────────────────────────────────
# Built at startup. Memory backend = single-instance (default, fully backwards
# compatible). Redis backend = HA mode with cross-instance affinity + relay.
INSTANCE_VERSION = "0.2.0"
INSTANCE_PORT = int(os.environ.get("HELEN_RENDEZVOUS_PORT", "8080"))
TUNNEL_REGISTRATION_TTL_SEC = int(os.environ.get("HELEN_RENDEZVOUS_TUNNEL_TTL", "60"))
SIGNAL_TTL_SEC = int(os.environ.get("HELEN_RENDEZVOUS_SIGNAL_TTL", "300"))
CROSS_INSTANCE_TIMEOUT_SEC = float(os.environ.get("HELEN_RENDEZVOUS_XINST_TIMEOUT", "25"))

backend: Any = None
instance_registry: Any = None
session_affinity: Any = None
cross_instance: Any = None


def _load_provider() -> dict[str, Any]:
    return {
        "tunnels_local": len(tunnels),
        "signals_local": len(signals),
        "inflight_total": sum(len(t.inflight) for t in tunnels.values()),
    }


def _tunnel_share_info(entry: TunnelEntry, owner_instance_id: str) -> dict[str, Any]:
    return {
        "public_id": entry.public_id,
        "name": entry.name,
        "connected_at": entry.connected_at,
        "owner_instance_id": owner_instance_id,
        "version": INSTANCE_VERSION,
    }


# ── Helpers ───────────────────────────────────────────────


def _require_token(auth_header: str | None = None, query_token: str | None = None) -> None:
    """Fail-closed token check. Raises HTTPException(401) on miss."""
    if not BOOTSTRAP_TOKEN:
        raise HTTPException(
            status_code=503,
            detail="rendezvous started without HELEN_RENDEZVOUS_TOKEN — refusing all requests",
        )
    candidates: list[str] = []
    if auth_header:
        if auth_header.lower().startswith("bearer "):
            candidates.append(auth_header[7:].strip())
        else:
            candidates.append(auth_header.strip())
    if query_token:
        candidates.append(query_token.strip())
    for c in candidates:
        if secrets.compare_digest(c, BOOTSTRAP_TOKEN):
            return
    raise HTTPException(status_code=401, detail="invalid or missing rendezvous token")


def _gen_public_id() -> str:
    """Short-but-unguessable tunnel id. 13 chars of base32 alphabet = ~64 bits."""
    return uuid.uuid4().hex[:13]


# ── FastAPI app ───────────────────────────────────────────


app = FastAPI(title="Helen-Rendezvous", version="0.1.0")


@app.get("/")
async def root() -> dict[str, Any]:
    return {
        "service": "Helen-Rendezvous",
        "version": "0.1.0",
        "tunnels": len(tunnels),
        "signals": len(signals),
        "token_configured": BOOTSTRAP_TOKEN is not None,
    }


@app.get("/health")
async def health() -> dict[str, Any]:
    """Liveness probe — same shape as Helen-Server /api/health and
    Helen-Router /router/health so verify-deployment.py + load
    balancers can probe all three with one pattern.

    Public (no token). Returns 200 on a sane process state, 503 if
    the bootstrap token isn't configured (the rendezvous is then
    refusing all real work)."""
    if BOOTSTRAP_TOKEN is None:
        return Response(
            content='{"status":"degraded","reason":"no bootstrap token"}',
            status_code=503,
            media_type="application/json",
        )
    return {
        "status": "ok",
        "service": "Helen-Rendezvous",
        "version": "0.1.0",
        "tunnels": len(tunnels),
        "signals": len(signals),
    }


@app.get("/status")
async def status() -> dict[str, Any]:
    out = []
    for t in tunnels.values():
        out.append({
            "public_id": t.public_id,
            "name": t.name,
            "connected_at": t.connected_at,
            "uptime_sec": int(time.time() - t.connected_at),
            "inflight": len(t.inflight),
        })
    return {"tunnels": out, "signals": list(signals.keys())}


# ── Reverse tunnel — server side (WS) ─────────────────────


@app.websocket("/tunnel/register")
async def tunnel_register(ws: WebSocket):
    """Helen-Server calls this and keeps the socket open forever.

    Wire protocol (text JSON frames):

    Server→Rendezvous:
      {"type": "hello", "name": "<display name>"}           once, right after accept
      {"type": "response", "rid": "<id>", "status": 200,
       "headers": [[k, v], ...], "body_b64": "..."}
      {"type": "ping"}                                       keepalive

    Rendezvous→Server:
      {"type": "welcome", "public_id": "...", "client_url": "..."}
      {"type": "request", "rid": "<id>", "method": "GET",
       "path": "/api/health", "headers": [...], "body_b64": "..."}
      {"type": "pong"}
    """
    token = ws.query_params.get("token")
    if not BOOTSTRAP_TOKEN or not token or not secrets.compare_digest(token, BOOTSTRAP_TOKEN):
        await ws.close(code=4401)  # application-level auth failure
        return

    await ws.accept()

    # First frame must be a hello — it carries the server's display name.
    # Capture the peer's address so a dead/slow handshake leaves a trail in
    # the rendezvous logs (was previously silent, blind operational spot).
    peer_ip = ws.client.host if ws.client else "?"
    try:
        hello_text = await asyncio.wait_for(ws.receive_text(), timeout=10)
    except asyncio.TimeoutError:
        logger.warning("tunnel_hello_timeout", peer_ip=peer_ip)
        await ws.close(code=4000)
        return
    try:
        hello = json.loads(hello_text)
    except Exception as exc:
        logger.warning("tunnel_hello_invalid_json", peer_ip=peer_ip, error=str(exc))
        await ws.close(code=4000)
        return
    if not isinstance(hello, dict) or hello.get("type") != "hello":
        logger.warning("tunnel_hello_invalid_shape", peer_ip=peer_ip)
        await ws.close(code=4000)
        return

    name = str(hello.get("name") or "Helen Server")[:120]
    public_id = _gen_public_id()

    entry = TunnelEntry(public_id=public_id, name=name, websocket=ws)
    async with _registry_lock:
        tunnels[public_id] = entry

    # Cluster integration: write this tunnel into the shared backend so other
    # rendezvous instances can route requests to us. Also bind affinity.
    owner_id = instance_registry.instance_id if instance_registry is not None else "local"
    if backend is not None:
        with contextlib.suppress(Exception):
            await backend.register_tunnel(
                public_id,
                _tunnel_share_info(entry, owner_id),
                TUNNEL_REGISTRATION_TTL_SEC,
            )
    if session_affinity is not None:
        with contextlib.suppress(Exception):
            await session_affinity.bind(public_id, owner_id, extra={"name": name})

    # Build a user-facing URL suggestion. Rendezvous doesn't know its own
    # public hostname, so we just echo a path template; operator plugs in
    # the real host on the server side via the admin URL field.
    welcome = {
        "type": "welcome",
        "public_id": public_id,
        "path_template": f"/t/{public_id}/<your path>",
    }
    await entry.send_json(welcome)

    # Periodic TTL refresher for the shared tunnel registration.
    async def _refresh_shared_registration() -> None:
        if backend is None:
            return
        try:
            while True:
                await asyncio.sleep(max(5, TUNNEL_REGISTRATION_TTL_SEC // 3))
                with contextlib.suppress(Exception):
                    await backend.refresh_tunnel(public_id, TUNNEL_REGISTRATION_TTL_SEC)
                if session_affinity is not None:
                    with contextlib.suppress(Exception):
                        await session_affinity.refresh(public_id, owner_id)
        except asyncio.CancelledError:
            return

    refresh_task = asyncio.create_task(_refresh_shared_registration())

    try:
        while True:
            text = await ws.receive_text()
            try:
                frame = json.loads(text)
            except Exception:
                continue
            if not isinstance(frame, dict):
                continue
            ftype = frame.get("type")
            entry.last_seen = time.time()
            if ftype == "ping":
                await entry.send_json({"type": "pong"})
            elif ftype == "response":
                rid = frame.get("rid")
                fut = entry.inflight.pop(str(rid), None) if rid else None
                if fut is not None and not fut.done():
                    fut.set_result(frame)
            elif ftype in ("ws_frame", "ws_close"):
                # Hand off to the proxied external WebSocket.
                wsid = str(frame.get("wsid") or "")
                q = entry.ws_sessions.get(wsid)
                if q is not None:
                    try:
                        q.put_nowait(frame)
                    except asyncio.QueueFull:
                        # Dead slow consumer — tear it down.
                        entry.ws_sessions.pop(wsid, None)
            elif ftype == "bye":
                break
            # silently drop unknown frames
    except WebSocketDisconnect:
        pass
    except Exception as e:  # pragma: no cover
        with contextlib.suppress(Exception):
            await ws.close(code=1011, reason=str(e)[:100])
    finally:
        refresh_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await refresh_task
        async with _registry_lock:
            tunnels.pop(public_id, None)
        # Cancel any inflight requests so the HTTP side returns 502.
        for fut in list(entry.inflight.values()):
            if not fut.done():
                fut.set_exception(RuntimeError("tunnel closed"))
        # Best-effort: remove the shared registration + affinity.
        if backend is not None:
            with contextlib.suppress(Exception):
                await backend.unregister_tunnel(public_id)
        if session_affinity is not None:
            with contextlib.suppress(Exception):
                await session_affinity.release(public_id)


# ── Reverse tunnel — client side (HTTP proxy) ─────────────


@app.api_route("/t/{public_id}/{rest:path}",
               methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
async def tunnel_proxy(public_id: str, rest: str, request: Request) -> Response:
    entry = tunnels.get(public_id)
    if entry is None:
        # Cluster fallback: maybe another rendezvous instance holds the WS.
        owner = await _resolve_owner(public_id)
        if owner and instance_registry is not None and owner != instance_registry.instance_id:
            return await _cross_instance_http_proxy(public_id, rest, request, owner)
        raise HTTPException(status_code=404, detail="unknown tunnel id")

    if len(entry.inflight) >= TUNNEL_MAX_INFLIGHT:
        raise HTTPException(status_code=503,
                            detail="tunnel backhaul is saturated, try again")

    import base64 as _b64

    body = await request.body()
    # Strip hop-by-hop headers before forwarding. Preserve query string.
    skip = {"host", "connection", "keep-alive", "proxy-authenticate",
            "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade"}
    headers_out: list[list[str]] = []
    for k, v in request.headers.items():
        if k.lower() not in skip:
            headers_out.append([k, v])

    rid = uuid.uuid4().hex[:16]
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    entry.inflight[rid] = fut

    target_path = "/" + rest
    if request.url.query:
        target_path += "?" + request.url.query

    await entry.send_json({
        "type": "request",
        "rid": rid,
        "method": request.method,
        "path": target_path,
        "headers": headers_out,
        "body_b64": _b64.b64encode(body).decode("ascii") if body else "",
    })

    try:
        frame = await asyncio.wait_for(fut, timeout=TUNNEL_REQUEST_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        entry.inflight.pop(rid, None)
        raise HTTPException(status_code=504, detail="tunnel backhaul timed out")
    except Exception as e:
        entry.inflight.pop(rid, None)
        raise HTTPException(status_code=502, detail=f"tunnel error: {e}")

    status = int(frame.get("status") or 502)
    headers_in = frame.get("headers") or []
    body_b64 = frame.get("body_b64") or ""
    body_bytes = _b64.b64decode(body_b64.encode("ascii")) if body_b64 else b""
    resp_headers: list[tuple[str, str]] = []
    for pair in headers_in:
        if isinstance(pair, list) and len(pair) == 2:
            name, value = str(pair[0]), str(pair[1])
            if name.lower() in skip:
                continue
            resp_headers.append((name, value))
    return Response(content=body_bytes, status_code=status, headers=dict(resp_headers))


# ── Reverse tunnel — WebSocket proxy ──────────────────────
#
# Frames the rendezvous sends to the Helen-Server tunnel peer for WS proxy:
#   {"type": "ws_open",  "wsid": "<id>", "path": "/socket.io/?EIO=4...",
#    "headers": [[k, v], ...]}
#   {"type": "ws_frame", "wsid": "<id>", "kind": "text|binary",
#    "data": "...", "data_b64": "..."}
#   {"type": "ws_close", "wsid": "<id>", "code": 1000, "reason": ""}
#
# The server replies with the same frame shape in the reverse direction.
# This gives Socket.IO's websocket transport full fidelity through the
# rendezvous; clients don't need to be aware they're tunneled.


@app.websocket("/t/{public_id}/{rest:path}")
async def tunnel_ws_proxy(ws: WebSocket, public_id: str, rest: str):
    entry = tunnels.get(public_id)
    if entry is None:
        await ws.close(code=4404)
        return
    await ws.accept()

    import uuid as _uuid
    wsid = _uuid.uuid4().hex[:16]
    q: asyncio.Queue = asyncio.Queue(maxsize=256)
    entry.ws_sessions[wsid] = q

    # Forward handshake context (path + selected request headers) so the
    # server-side httpx-ws client can reconstruct a faithful upgrade.
    skip_headers = {"host", "connection", "upgrade", "sec-websocket-key",
                    "sec-websocket-version", "sec-websocket-accept",
                    "sec-websocket-extensions"}
    forward_headers = [
        [k, v] for k, v in ws.headers.items()
        if k.lower() not in skip_headers
    ]
    target_path = "/" + rest
    if ws.url.query:
        target_path += "?" + ws.url.query

    await entry.send_json({
        "type": "ws_open", "wsid": wsid,
        "path": target_path, "headers": forward_headers,
    })

    async def client_to_tunnel() -> None:
        """External client → our WS → tunnel → server."""
        import base64 as _b64
        try:
            while True:
                msg = await ws.receive()
                if msg.get("type") == "websocket.disconnect":
                    break
                if "text" in msg and msg["text"] is not None:
                    await entry.send_json({
                        "type": "ws_frame", "wsid": wsid,
                        "kind": "text", "data": msg["text"],
                    })
                elif "bytes" in msg and msg["bytes"] is not None:
                    await entry.send_json({
                        "type": "ws_frame", "wsid": wsid,
                        "kind": "binary",
                        "data_b64": _b64.b64encode(msg["bytes"]).decode("ascii"),
                    })
        except Exception:
            pass
        finally:
            # Tell the server we're done — it should close the inner WS.
            with contextlib.suppress(Exception):
                await entry.send_json({
                    "type": "ws_close", "wsid": wsid,
                    "code": 1000, "reason": "",
                })

    async def tunnel_to_client() -> None:
        """Server → tunnel → our WS → external client."""
        import base64 as _b64
        try:
            while True:
                frame = await q.get()
                ft = frame.get("type")
                if ft == "ws_close":
                    return
                if ft != "ws_frame":
                    continue
                kind = frame.get("kind")
                if kind == "text":
                    await ws.send_text(frame.get("data") or "")
                else:
                    data = frame.get("data_b64") or ""
                    await ws.send_bytes(_b64.b64decode(data.encode("ascii")) if data else b"")
        except Exception:
            pass

    try:
        # Both directions run until one side hangs up. If the server WS
        # vanishes, the entry's ws_sessions gets cleaned up by the disconnect
        # handler above, and `tunnel_to_client` unblocks via ws_close.
        t1 = asyncio.create_task(client_to_tunnel())
        t2 = asyncio.create_task(tunnel_to_client())
        done, pending = await asyncio.wait(
            {t1, t2}, return_when=asyncio.FIRST_COMPLETED,
        )
        for p in pending:
            p.cancel()
    finally:
        entry.ws_sessions.pop(wsid, None)
        with contextlib.suppress(Exception):
            await ws.close()


# ── Hole-punch signaling ──────────────────────────────────


@app.post("/signal/register")
async def signal_register(request: Request) -> dict[str, Any]:
    _require_token(request.headers.get("Authorization"))
    body = await request.json()
    pid = str(body.get("public_id") or "").strip()
    udp = str(body.get("udp_endpoint") or "").strip()
    if not pid or not udp or ":" not in udp:
        raise HTTPException(status_code=400, detail="public_id and udp_endpoint required")
    # Record what the client says its endpoint is + what we observed.
    observed_ip = request.client.host if request.client else ""
    meta = body.get("meta") if isinstance(body.get("meta"), dict) else {}
    async with _registry_lock:
        signals[pid] = SignalEntry(
            public_id=pid, udp_endpoint=udp,
            meta={**meta, "observed_ip": observed_ip},
        )
    # Shared cluster store — best-effort, single-instance keeps working if it fails.
    if backend is not None:
        with contextlib.suppress(Exception):
            await backend.register_signal(
                f"endpoint:{pid}",
                {
                    "public_id": pid,
                    "udp_endpoint": udp,
                    "meta": {**meta, "observed_ip": observed_ip},
                    "updated_at": time.time(),
                },
                SIGNAL_TTL_SEC,
            )
    return {"ok": True, "observed_ip": observed_ip}


@app.get("/signal/lookup/{public_id}")
async def signal_lookup(public_id: str, request: Request) -> dict[str, Any]:
    _require_token(request.headers.get("Authorization"))
    entry = signals.get(public_id)
    if entry is None and backend is not None:
        # Cluster fallback: another instance may have written this signal.
        shared = None
        with contextlib.suppress(Exception):
            shared = await backend.lookup_signal(f"endpoint:{public_id}")
        if shared:
            updated_at = float(shared.get("updated_at") or time.time())
            return {
                "public_id": shared.get("public_id") or public_id,
                "udp_endpoint": shared.get("udp_endpoint") or "",
                "updated_at": updated_at,
                "age_sec": int(time.time() - updated_at),
                "meta": shared.get("meta") or {},
            }
    if not entry:
        raise HTTPException(status_code=404, detail="no such registration")
    return {
        "public_id": entry.public_id,
        "udp_endpoint": entry.udp_endpoint,
        "updated_at": entry.updated_at,
        "age_sec": int(time.time() - entry.updated_at),
        "meta": entry.meta,
    }


@app.get("/signal/whoami")
async def signal_whoami(request: Request) -> dict[str, Any]:
    """STUN-like self-endpoint discovery over HTTP. Useful pre-hole-punch to
    learn your own post-NAT IP. Not UDP-accurate for symmetric NATs, but
    good enough for cone NATs which cover the vast majority of home routers."""
    _require_token(request.headers.get("Authorization"))
    ip = request.client.host if request.client else ""
    return {"observed_ip": ip}


# ── Blind TCP relay (last-resort) ─────────────────────────


async def _join_streams(reader_a: asyncio.StreamReader, writer_a: asyncio.StreamWriter,
                        reader_b: asyncio.StreamReader, writer_b: asyncio.StreamWriter) -> None:
    """Bidirectional byte-by-byte pipe between two sockets. Closes both when
    either direction hits EOF or errors."""
    async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    break
                writer.write(chunk)
                await writer.drain()
        except (asyncio.CancelledError, ConnectionError, OSError):
            pass
        finally:
            with contextlib.suppress(Exception):
                writer.close()

    await asyncio.gather(
        _pipe(reader_a, writer_b),
        _pipe(reader_b, writer_a),
    )


class RelayHub:
    """Waits for matching REGISTER/LOOKUP pairs and joins them."""

    def __init__(self) -> None:
        # public_id -> deque of pending backend (Helen-Server) streams
        self._backends: dict[str, list[tuple[asyncio.StreamReader, asyncio.StreamWriter]]] = {}
        self._lock = asyncio.Lock()

    async def handle_backend(self, reader: asyncio.StreamReader,
                             writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=5.0)
        except asyncio.TimeoutError:
            writer.close(); return
        parts = line.decode("ascii", errors="replace").strip().split()
        if len(parts) != 2 or parts[0] != "REGISTER":
            writer.write(b"ERR bad greeting\n"); await writer.drain(); writer.close(); return
        token = writer.get_extra_info("peercert") or None  # placeholder
        if not BOOTSTRAP_TOKEN:
            writer.write(b"ERR server not configured\n"); await writer.drain(); writer.close(); return
        pid = parts[1]
        writer.write(b"OK waiting\n"); await writer.drain()

        async with self._lock:
            self._backends.setdefault(pid, []).append((reader, writer))

        # Park — caller stays connected. When a frontend arrives and claims
        # us, _pair() will consume the stream. If we're still here after,
        # say, 5 minutes, time out and drop the registration.
        try:
            await asyncio.wait_for(_wait_until_closed(reader), timeout=300)
        except asyncio.TimeoutError:
            pass
        async with self._lock:
            queue = self._backends.get(pid, [])
            self._backends[pid] = [pair for pair in queue if pair[0] is not reader]
        with contextlib.suppress(Exception):
            writer.close()

    async def handle_frontend(self, reader: asyncio.StreamReader,
                              writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=5.0)
        except asyncio.TimeoutError:
            writer.close(); return
        parts = line.decode("ascii", errors="replace").strip().split()
        if len(parts) != 2 or parts[0] != "LOOKUP":
            writer.write(b"ERR bad greeting\n"); await writer.drain(); writer.close(); return
        pid = parts[1]

        async with self._lock:
            queue = self._backends.get(pid, [])
            if not queue:
                writer.write(b"ERR no backend registered\n")
                await writer.drain(); writer.close(); return
            back_reader, back_writer = queue.pop(0)

        writer.write(b"OK joined\n"); await writer.drain()
        try:
            back_writer.write(b"GO\n"); await back_writer.drain()
        except Exception:
            writer.close(); return
        await _join_streams(reader, writer, back_reader, back_writer)


async def _wait_until_closed(reader: asyncio.StreamReader) -> None:
    while not reader.at_eof():
        try:
            data = await reader.read(1024)
            if not data:
                return
        except Exception:
            return


relay_hub = RelayHub()


async def _run_relay_listeners() -> None:
    backend_srv = await asyncio.start_server(
        relay_hub.handle_backend, "0.0.0.0", RELAY_BACKEND_PORT,
    )
    frontend_srv = await asyncio.start_server(
        relay_hub.handle_frontend, "0.0.0.0", RELAY_FRONTEND_PORT,
    )
    async with backend_srv, frontend_srv:
        await asyncio.gather(backend_srv.serve_forever(), frontend_srv.serve_forever())


async def _resolve_owner(public_id: str) -> str | None:
    """Find which rendezvous instance owns a peer's WebSocket."""
    if session_affinity is None:
        return None
    with contextlib.suppress(Exception):
        owner = await session_affinity.owner_of(public_id)
        if owner:
            return owner
    if backend is None:
        return None
    with contextlib.suppress(Exception):
        info = await backend.lookup_tunnel(public_id)
        if info:
            return str(info.get("owner_instance_id") or "") or None
    return None


async def _cross_instance_http_proxy(
    public_id: str,
    rest: str,
    request: Request,
    owner: str,
) -> Response:
    """Forward an HTTP request to the instance that owns this tunnel."""
    if cross_instance is None:
        raise HTTPException(status_code=502, detail="cross-instance relay disabled")
    import base64 as _b64
    body = await request.body()
    skip = {"host", "connection", "keep-alive", "proxy-authenticate",
            "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade"}
    headers_out = [[k, v] for k, v in request.headers.items() if k.lower() not in skip]
    target_path = "/" + rest
    if request.url.query:
        target_path += "?" + request.url.query
    payload = {
        "method": request.method,
        "path": target_path,
        "headers": headers_out,
        "body_b64": _b64.b64encode(body).decode("ascii") if body else "",
    }
    try:
        resp = await cross_instance.request(
            kind="tunnel_request",
            to_instance=owner,
            peer_id=public_id,
            payload=payload,
            timeout=CROSS_INSTANCE_TIMEOUT_SEC,
        )
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="cross-instance relay timed out")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"cross-instance relay error: {e}")
    status = int(resp.get("status") or 502)
    headers_in = resp.get("headers") or []
    body_b64 = resp.get("body_b64") or ""
    body_bytes = _b64.b64decode(body_b64.encode("ascii")) if body_b64 else b""
    resp_headers: dict[str, str] = {}
    for pair in headers_in:
        if isinstance(pair, list) and len(pair) == 2:
            name, value = str(pair[0]), str(pair[1])
            if name.lower() in skip:
                continue
            resp_headers[name] = value
    return Response(content=body_bytes, status_code=status, headers=resp_headers)


async def _handle_cross_instance_tunnel_request(envelope: dict[str, Any]) -> None:
    """Other instance asked us to proxy a request to our local tunnel."""
    import base64 as _b64
    peer_id = str(envelope.get("peer_id") or "")
    msg_id = str(envelope.get("msg_id") or "")
    from_inst = str(envelope.get("from_instance") or "")
    payload = envelope.get("payload") or {}
    entry = tunnels.get(peer_id)
    if entry is None:
        if cross_instance is not None:
            await cross_instance.respond(
                msg_id, from_inst, peer_id,
                {"status": 404, "headers": [], "body_b64": ""},
            )
        return
    if len(entry.inflight) >= TUNNEL_MAX_INFLIGHT:
        if cross_instance is not None:
            await cross_instance.respond(
                msg_id, from_inst, peer_id,
                {"status": 503, "headers": [], "body_b64": ""},
            )
        return
    rid = uuid.uuid4().hex[:16]
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    entry.inflight[rid] = fut
    await entry.send_json({
        "type": "request",
        "rid": rid,
        "method": payload.get("method") or "GET",
        "path": payload.get("path") or "/",
        "headers": payload.get("headers") or [],
        "body_b64": payload.get("body_b64") or "",
    })
    try:
        frame = await asyncio.wait_for(fut, timeout=TUNNEL_REQUEST_TIMEOUT_SEC)
    except Exception:
        entry.inflight.pop(rid, None)
        if cross_instance is not None:
            await cross_instance.respond(
                msg_id, from_inst, peer_id,
                {"status": 504, "headers": [], "body_b64": ""},
            )
        return
    if cross_instance is not None:
        await cross_instance.respond(
            msg_id, from_inst, peer_id,
            {
                "status": int(frame.get("status") or 502),
                "headers": frame.get("headers") or [],
                "body_b64": frame.get("body_b64") or "",
            },
        )


@app.on_event("startup")
async def _startup() -> None:
    """Bring up the TCP blind relay + (optionally) the cluster stack."""
    asyncio.create_task(_run_relay_listeners())
    await _startup_cluster()


async def _startup_cluster() -> None:
    """Initialise storage backend + instance registry + cross-instance relay."""
    global backend, instance_registry, session_affinity, cross_instance
    if not _CLUSTER_AVAILABLE or build_backend is None:
        logger.info("cluster_disabled", reason="modules unavailable")
        return
    try:
        backend = build_backend()
        # The MemoryBackend has a reaper that must be started.
        if hasattr(backend, "start"):
            await backend.start()
    except Exception as exc:
        logger.warning("cluster_backend_init_failed", error=str(exc))
        backend = MemoryBackend() if MemoryBackend is not None else None
        if backend is not None and hasattr(backend, "start"):
            with contextlib.suppress(Exception):
                await backend.start()
    if backend is None:
        return
    instance_registry = InstanceRegistry(
        backend,
        port=INSTANCE_PORT,
        version=INSTANCE_VERSION,
    )
    instance_registry.set_load_provider(_load_provider)
    await instance_registry.start_heartbeat()
    session_affinity = SessionAffinity(backend, ttl=TUNNEL_REGISTRATION_TTL_SEC)
    cross_instance = CrossInstanceRelay(backend, instance_registry.instance_id)
    cross_instance.on("tunnel_request", _handle_cross_instance_tunnel_request)
    await cross_instance.start()
    logger.info(
        "cluster_started",
        instance_id=instance_registry.instance_id,
        backend=getattr(backend, "backend_name", "?"),
    )


@app.on_event("shutdown")
async def _shutdown() -> None:
    """Best-effort graceful teardown of cluster + backend connections."""
    global backend, instance_registry, session_affinity, cross_instance
    if cross_instance is not None:
        with contextlib.suppress(Exception):
            await cross_instance.stop()
    if instance_registry is not None:
        with contextlib.suppress(Exception):
            await instance_registry.stop_heartbeat()
    if backend is not None:
        with contextlib.suppress(Exception):
            await backend.close()
    logger.info("rendezvous_shutdown_complete")


# Admin / cluster routes — imported here so they can see the module-level
# globals. Failure to import (e.g. starlette missing in dev) is logged and the
# rest of the app keeps running.
try:
    from admin_routes import register_admin_routes  # noqa: E402
    register_admin_routes(app)
except Exception as _adm_exc:  # pragma: no cover
    logger.warning("admin_routes_unavailable", error=str(_adm_exc))
