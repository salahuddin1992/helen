"""
FederationDiagnostics — end-to-end and cross-peer diagnostic ops.

* ``diagnose(peer_id)``       — synthetic message → ack → hop summary.
* ``time_skew()``             — measure clock drift against each peer.
* ``cert_chain(peer_id)``     — proxy to ``FederationCertManager.validate_chain``.
* ``path_mtu(peer_id)``       — best-effort MTU probe.

Every routine is async, never raises, and degrades to a structured
``{ok: false, error: ...}`` payload if the network layer is missing.

Singleton: ``get_diagnostics()``.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

import structlog
from sqlalchemy import select

from app.db.session import async_session_factory
from app.models.federation_v2 import FederatedServer
from app.services.federation_v2.cert_manager import get_cert_manager

logger = structlog.get_logger(__name__)


CLOCK_SKEW_WARN_MS = 500
DEFAULT_PROBE_TIMEOUT = 5.0


class FederationDiagnostics:
    # ── per-peer e2e diagnose ────────────────────────────────

    async def diagnose(
        self,
        peer_id: str,
        timeout: float = DEFAULT_PROBE_TIMEOUT,
    ) -> dict[str, Any]:
        async with async_session_factory() as db:
            row = (await db.execute(
                select(FederatedServer).where(FederatedServer.id == peer_id)
            )).scalar_one_or_none()
            if row is None:
                row = (await db.execute(
                    select(FederatedServer).where(
                        FederatedServer.server_id == peer_id
                    )
                )).scalar_one_or_none()
        if row is None:
            return {"ok": False, "error": "not_found"}

        hops: list[dict[str, Any]] = []
        started = time.perf_counter()
        url = row.advertise_url.rstrip("/") + "/api/_federation/v2/ping"
        try:
            import httpx
        except Exception:
            return {
                "ok": False, "error": "httpx_unavailable",
                "server_id": row.server_id,
            }

        # Hop 1 — DNS
        dns_start = time.perf_counter()
        dns_ok = True
        try:
            import socket
            host = row.advertise_url.split("//", 1)[-1].split("/", 1)[0].split(":", 1)[0]
            socket.gethostbyname(host)
        except Exception as exc:
            dns_ok = False
            hops.append({
                "hop": "dns", "ok": False, "error": str(exc),
                "elapsed_ms": int((time.perf_counter() - dns_start) * 1000),
            })
        if dns_ok:
            hops.append({
                "hop": "dns", "ok": True,
                "elapsed_ms": int((time.perf_counter() - dns_start) * 1000),
            })

        # Hop 2 — TCP/TLS connect (httpx implicit) + first byte
        rtt_ms = -1
        status = 0
        try:
            async with httpx.AsyncClient(timeout=timeout) as cli:
                t0 = time.perf_counter()
                r = await cli.get(url)
                rtt_ms = int((time.perf_counter() - t0) * 1000)
                status = r.status_code
            hops.append({
                "hop": "http", "ok": 200 <= status < 500,
                "status": status, "elapsed_ms": rtt_ms,
            })
        except Exception as exc:
            hops.append({
                "hop": "http", "ok": False, "error": str(exc),
                "elapsed_ms": int((time.perf_counter() - dns_start) * 1000),
            })

        # Hop 3 — cert chain
        chain = await get_cert_manager().validate_chain(row.server_id)
        hops.append({"hop": "cert", **{k: v for k, v in chain.items() if k != "ok"},
                     "ok": chain.get("ok", False)})

        total = int((time.perf_counter() - started) * 1000)
        return {
            "ok":         all(h.get("ok") for h in hops),
            "server_id":  row.server_id,
            "advertise_url": row.advertise_url,
            "rtt_ms":     rtt_ms,
            "total_ms":   total,
            "hops":       hops,
            "timestamp":  time.time(),
        }

    # ── cluster-wide ─────────────────────────────────────────

    async def time_skew(self, timeout: float = 3.0) -> dict[str, Any]:
        async with async_session_factory() as db:
            peers = (await db.execute(select(FederatedServer))).scalars().all()
        try:
            import httpx
        except Exception:
            return {"ok": False, "error": "httpx_unavailable", "peers": []}

        results: list[dict[str, Any]] = []

        async def _probe(p: FederatedServer) -> dict[str, Any]:
            url = p.advertise_url.rstrip("/") + "/api/_federation/v2/time"
            t_send = time.time()
            try:
                async with httpx.AsyncClient(timeout=timeout) as cli:
                    r = await cli.get(url)
                t_recv = time.time()
                if r.status_code != 200:
                    return {
                        "server_id": p.server_id, "ok": False,
                        "error": f"http_{r.status_code}",
                    }
                data = r.json() if r.content else {}
                remote = float(data.get("now") or 0)
                rtt = (t_recv - t_send) / 2.0
                local = (t_send + t_recv) / 2.0
                skew_ms = int((remote - local) * 1000)
                return {
                    "server_id": p.server_id,
                    "ok":        True,
                    "skew_ms":   skew_ms,
                    "warn":      abs(skew_ms) > CLOCK_SKEW_WARN_MS,
                    "rtt_ms":    int(rtt * 1000),
                }
            except Exception as exc:
                return {
                    "server_id": p.server_id, "ok": False, "error": str(exc),
                }

        results = await asyncio.gather(*[_probe(p) for p in peers])
        warned = [r for r in results if r.get("warn")]
        return {
            "ok":      True,
            "checked": len(results),
            "warned":  len(warned),
            "warn_threshold_ms": CLOCK_SKEW_WARN_MS,
            "peers":   results,
        }

    async def cert_chain(self, peer_id: str) -> dict[str, Any]:
        return await get_cert_manager().validate_chain(peer_id)

    async def path_mtu(
        self,
        peer_id: str,
        max_mtu: int = 1500,
        min_mtu: int = 576,
    ) -> dict[str, Any]:
        """Coarse path-MTU probe: scan a few common sizes."""
        async with async_session_factory() as db:
            row = (await db.execute(
                select(FederatedServer).where(
                    FederatedServer.server_id == peer_id
                )
            )).scalar_one_or_none()
            if row is None:
                row = (await db.execute(
                    select(FederatedServer).where(FederatedServer.id == peer_id)
                )).scalar_one_or_none()
        if row is None:
            return {"ok": False, "error": "not_found"}
        try:
            import httpx
        except Exception:
            return {"ok": False, "error": "httpx_unavailable"}

        sizes = [s for s in (min_mtu, 1200, 1400, max_mtu) if min_mtu <= s <= max_mtu]
        seen: list[dict[str, Any]] = []
        best = 0
        for sz in sizes:
            payload = "x" * max(0, sz - 200)
            t0 = time.perf_counter()
            try:
                async with httpx.AsyncClient(timeout=DEFAULT_PROBE_TIMEOUT) as cli:
                    r = await cli.post(
                        row.advertise_url.rstrip("/") + "/api/_federation/v2/mtu",
                        content=payload,
                    )
                seen.append({
                    "size": sz, "ok": r.status_code < 500,
                    "status": r.status_code,
                    "elapsed_ms": int((time.perf_counter() - t0) * 1000),
                })
                if r.status_code < 500:
                    best = max(best, sz)
            except Exception as exc:
                seen.append({"size": sz, "ok": False, "error": str(exc)})
        return {
            "ok":         best > 0,
            "server_id":  row.server_id,
            "path_mtu":   best,
            "probes":     seen,
        }


# ── singleton ───────────────────────────────────────────────


_diag: Optional[FederationDiagnostics] = None


def get_diagnostics() -> FederationDiagnostics:
    global _diag
    if _diag is None:
        _diag = FederationDiagnostics()
    return _diag
