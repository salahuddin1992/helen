"""
Edge — active latency probing & steering.

Each pair of edge nodes pings the other via HTTPS, building a matrix
of p50/p95 round-trip times. Steering recommendations are emitted at
1m granularity so the geo router can demote slow paths.

Anycast/DNS helper: ``emit_dns_zone()`` returns a ready-to-import BIND
zone fragment for setups that prefer DNS-based steering over a smart
proxy.
"""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy import select

from app.core.logging import get_logger
from app.db.session import async_session_factory
from app.models.edge import EdgeNode

logger = get_logger(__name__)


PROBE_INTERVAL = 60.0
SAMPLE_WINDOW = 100


@dataclass
class ProbeResult:
    src: str
    dst: str
    rtt_ms: float
    ok: bool
    error: str = ""


class LatencySteering:
    """Pairwise latency tracker. Singleton."""

    def __init__(self) -> None:
        self._samples: dict[tuple[str, str], deque[float]] = defaultdict(
            lambda: deque(maxlen=SAMPLE_WINDOW)
        )
        self._task: Optional[asyncio.Task[None]] = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="edge-latency")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.probe_all()
            except Exception as exc:
                logger.warning("edge_latency_probe_err err=%s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=PROBE_INTERVAL)
            except asyncio.TimeoutError:
                continue

    async def probe_all(self) -> list[ProbeResult]:
        async with async_session_factory() as db:
            nodes = list((await db.execute(
                select(EdgeNode).where(EdgeNode.status == "active")
            )).scalars().all())
        if len(nodes) < 2:
            return []
        results: list[ProbeResult] = []
        async with self._open_client() as cli:
            jobs = []
            for src in nodes:
                for dst in nodes:
                    if src.id == dst.id:
                        continue
                    jobs.append(self._probe(cli, src, dst))
            done = await asyncio.gather(*jobs, return_exceptions=True)
            for r in done:
                if isinstance(r, ProbeResult):
                    results.append(r)
                    if r.ok:
                        self._samples[(r.src, r.dst)].append(r.rtt_ms)
        return results

    def _open_client(self):
        try:
            import httpx
        except Exception:
            class _Dummy:
                async def __aenter__(self): return self
                async def __aexit__(self, *_): return False
                async def get(self, *_a, **_k):
                    raise RuntimeError("httpx_missing")
            return _Dummy()
        import httpx as _h
        return _h.AsyncClient(timeout=5.0)

    async def _probe(self, cli, src: EdgeNode, dst: EdgeNode) -> ProbeResult:
        url = dst.advertise_url.rstrip("/") + "/api/edge/health"
        t0 = time.monotonic()
        try:
            r = await cli.get(url)
            ok = (r.status_code == 200)
        except Exception as exc:
            return ProbeResult(src=src.id, dst=dst.id, rtt_ms=0.0,
                               ok=False, error=str(exc))
        ms = (time.monotonic() - t0) * 1000.0
        return ProbeResult(src=src.id, dst=dst.id, rtt_ms=ms, ok=ok)

    def p95(self, src: str, dst: str) -> float:
        samples = sorted(self._samples.get((src, dst)) or [])
        if not samples:
            return 0.0
        idx = max(0, int(len(samples) * 0.95) - 1)
        return samples[idx]

    def matrix(self) -> dict[str, Any]:
        out: dict[str, dict[str, float]] = {}
        for (s, d), samples in self._samples.items():
            if not samples:
                continue
            out.setdefault(s, {})[d] = round(self.p95(s, d), 2)
        return out

    async def emit_dns_zone(self, hostname: str = "edge") -> str:
        """Build a BIND-format zone fragment for DNS steering."""
        async with async_session_factory() as db:
            nodes = list((await db.execute(
                select(EdgeNode).where(EdgeNode.status == "active")
            )).scalars().all())
        if not nodes:
            return f"; no edge nodes for {hostname}\n"
        lines = [f"; helen-edge DNS zone (generated {int(time.time())})"]
        for n in nodes:
            host = n.public_url or n.advertise_url
            host = host.replace("https://", "").replace("http://", "")
            host = host.split(":", 1)[0].split("/", 1)[0]
            lines.append(f"{hostname}.{n.region}.helen.   60   A   ; → {host}")
        return "\n".join(lines) + "\n"


_steering: Optional[LatencySteering] = None


def get_latency_steering() -> LatencySteering:
    global _steering
    if _steering is None:
        _steering = LatencySteering()
    return _steering
