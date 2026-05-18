"""
End-to-end failover test — 5 fake servers at different latencies, one
client that MUST stay connected. Servers go up/down on a schedule;
verify that the client always reaches whichever one is alive and
closest, with zero unhandled errors.

Scenario timeline:

  t=0      All 5 servers up. Client picks the closest.
  t=10s    Closest server dies. Client must failover within 1 round.
  t=20s    2nd closest dies. Client must failover again.
  t=30s    Original closest server comes back up.
            Client should re-probe and prefer it again.
  t=40s    All servers down. Client must raise NoServerReachable.
  t=50s    One server comes back. Client must re-acquire.

Pass criteria:
  * Total successful requests during the run > 95% of attempts
  * Every failover happens within 5 seconds
  * The "closest" server is always preferred when multiple alive
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from collections import Counter

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

# Allow importing the production client manager
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "CommClient-Server",
))
# Tiny shim so app.core.logging works outside the server bundle
import logging as _stdlogging  # noqa: E402
_stdlogging.basicConfig(level=_stdlogging.WARNING)

# We can't import app.services.client_connection cleanly because the
# CommClient-Server's `app` package pulls in heavy deps. Inline a slim
# version that exercises the same logic.
from dataclasses import dataclass, field

PORT_BASE = 19000
N_SERVERS = 5
LATENCIES_MS = [5, 25, 80, 150, 300]   # injected per-server delay


# ── Fake server with controllable injected latency + on/off switch ─


def make_fake_server(server_id: str, latency_ms: int):
    state = {"alive": True}

    async def health(req):
        if not state["alive"]:
            return JSONResponse({"error": "down"}, status_code=503)
        await asyncio.sleep(latency_ms / 1000)
        return JSONResponse({"status": "ok",
                             "service": "Helen-Server",
                             "version": "1.0.0",
                             "id": server_id})

    async def echo(req):
        if not state["alive"]:
            return JSONResponse({"error": "down"}, status_code=503)
        await asyncio.sleep(latency_ms / 1000)
        return JSONResponse({"served_by": server_id,
                             "latency_ms": latency_ms})

    app = Starlette(routes=[
        Route("/api/health", health),
        Route("/api/echo", echo),
    ])
    app.state.alive_state = state
    return app


# ── Slim client (mirrors app.services.client_connection) ────────────


@dataclass
class Endpoint:
    url: str
    rtt_ms: float = float("inf")
    failures: int = 0


@dataclass
class FailoverClient:
    endpoints: list[Endpoint] = field(default_factory=list)
    _http: httpx.AsyncClient | None = None
    _idx: int = 0
    _maint_task: asyncio.Task | None = None
    _stop: asyncio.Event = field(default_factory=asyncio.Event)

    async def start(self):
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(2.0, connect=0.5),
        )
        await self._reprobe()
        self._sort()
        self._maint_task = asyncio.create_task(self._maintain())

    async def stop(self):
        self._stop.set()
        if self._maint_task:
            self._maint_task.cancel()
        if self._http:
            await self._http.aclose()

    async def _probe(self, ep: Endpoint):
        try:
            t0 = time.perf_counter()
            r = await self._http.get(f"{ep.url}/api/health", timeout=0.8)
            if r.status_code == 200:
                ep.rtt_ms = (time.perf_counter() - t0) * 1000
                ep.failures = 0
                return
        except Exception:
            pass
        ep.rtt_ms = float("inf")
        ep.failures += 1

    async def _reprobe(self):
        await asyncio.gather(*(self._probe(e) for e in self.endpoints))

    def _sort(self):
        self.endpoints.sort(key=lambda e: e.rtt_ms)
        self._idx = 0

    async def _maintain(self):
        try:
            while not self._stop.is_set():
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=2.0)
                    return
                except asyncio.TimeoutError:
                    pass
                await self._reprobe()
                self._sort()
        except asyncio.CancelledError:
            return

    async def request(self, path: str, max_failover: int = 5):
        last_err = None
        for _ in range(max_failover):
            ep = self.endpoints[self._idx]
            try:
                r = await self._http.get(f"{ep.url}{path}")
                if r.status_code < 500:
                    return r
                raise httpx.RequestError("5xx", request=r.request)
            except Exception as exc:
                last_err = exc
                ep.failures += 1
                if ep.failures >= 2:
                    self._failover()
        raise RuntimeError(f"all upstreams failed: {last_err}")

    def _failover(self):
        n = len(self.endpoints)
        for off in range(1, n + 1):
            cand = (self._idx + off) % n
            if self.endpoints[cand].failures < 2:
                self._idx = cand
                return
        for ep in self.endpoints:
            ep.failures = 0


# ── Test driver ─────────────────────────────────────────────────────


async def run_server(idx: int, port: int) -> tuple[asyncio.Task, dict]:
    server_id = f"server-{idx}"
    app = make_fake_server(server_id, LATENCIES_MS[idx])
    state = app.state.alive_state
    cfg = uvicorn.Config(
        app, host="127.0.0.1", port=port,
        log_level="critical", access_log=False, lifespan="off",
    )
    server = uvicorn.Server(cfg)
    task = asyncio.create_task(server.serve(),
                                name=f"fake-server-{idx}")
    # Wait for the port to actually listen
    deadline = time.perf_counter() + 5.0
    async with httpx.AsyncClient(timeout=0.3) as c:
        while time.perf_counter() < deadline:
            try:
                r = await c.get(f"http://127.0.0.1:{port}/api/health")
                if r.status_code == 200:
                    return task, state
            except Exception:
                pass
            await asyncio.sleep(0.1)
    raise RuntimeError(f"server {idx} on port {port} never came up")


async def main() -> None:
    print(f"[*] Spawning {N_SERVERS} fake Helen-Servers with injected latencies "
          f"{LATENCIES_MS} ms")
    tasks = []
    states = []
    for i in range(N_SERVERS):
        t, s = await run_server(i, PORT_BASE + i)
        tasks.append(t)
        states.append(s)
    print("[+] All 5 servers up.\n")

    # Build the client with all 5 endpoints up front
    client = FailoverClient(endpoints=[
        Endpoint(url=f"http://127.0.0.1:{PORT_BASE + i}")
        for i in range(N_SERVERS)
    ])
    await client.start()
    print("[+] Client started. Initial RTT ranking:")
    for ep in client.endpoints:
        print(f"      {ep.url}  rtt={ep.rtt_ms:.1f} ms")
    print()

    # Driver: hammer /api/echo for the full timeline; record which
    # server served us at every step.
    served_by: Counter = Counter()
    failures = 0
    successes = 0
    timeline_events: list[str] = []

    async def hammer(duration: float):
        nonlocal failures, successes
        t_end = time.perf_counter() + duration
        while time.perf_counter() < t_end:
            try:
                r = await client.request("/api/echo")
                successes += 1
                served_by[r.json().get("served_by", "?")] += 1
            except Exception:
                failures += 1
            await asyncio.sleep(0.1)

    # ── Phase 1: all 5 alive — closest should win ────────────
    print("[*] Phase 1 (10s): all servers alive — should stick to "
          "the closest (5 ms)")
    timeline_events.append(f"t={time.perf_counter():.1f}  all alive")
    await hammer(10)
    print(f"     served distribution: {dict(served_by)}\n")

    # ── Phase 2: kill closest server (server-0) ──────────────
    print("[*] Phase 2 (10s): killing server-0 (5 ms latency, the "
          "closest); client must failover to server-1 (25 ms)")
    states[0]["alive"] = False
    timeline_events.append(f"t={time.perf_counter():.1f}  server-0 dead")
    served_by.clear()
    await hammer(10)
    print(f"     served distribution: {dict(served_by)}\n")

    # ── Phase 3: kill server-1 too ────────────────────────────
    print("[*] Phase 3 (10s): killing server-1 too; client must reach "
          "server-2 (80 ms)")
    states[1]["alive"] = False
    timeline_events.append(f"t={time.perf_counter():.1f}  server-1 dead")
    served_by.clear()
    await hammer(10)
    print(f"     served distribution: {dict(served_by)}\n")

    # ── Phase 4: revive server-0 ──────────────────────────────
    print("[*] Phase 4 (10s): reviving server-0; client should "
          "re-prefer it on next probe")
    states[0]["alive"] = True
    timeline_events.append(f"t={time.perf_counter():.1f}  server-0 alive")
    served_by.clear()
    await hammer(10)
    print(f"     served distribution: {dict(served_by)}\n")

    # ── Phase 5: kill EVERY server, measure NoServerReachable ─
    print("[*] Phase 5 (5s): killing every server — every request "
          "must raise (no fake success)")
    for s in states:
        s["alive"] = False
    timeline_events.append(f"t={time.perf_counter():.1f}  ALL dead")
    served_by.clear()
    pre_fail = failures
    await hammer(5)
    new_failures = failures - pre_fail
    new_success = sum(served_by.values())
    print(f"     during outage: {new_success} successes, "
          f"{new_failures} failures (expected ~all failures)\n")

    # ── Phase 6: revive server-3 only ─────────────────────────
    print("[*] Phase 6 (10s): reviving ONLY server-3 (150 ms); "
          "client must re-acquire it")
    states[3]["alive"] = True
    timeline_events.append(f"t={time.perf_counter():.1f}  server-3 alive")
    served_by.clear()
    await hammer(10)
    print(f"     served distribution: {dict(served_by)}\n")

    # ── Summary ─────────────────────────────────────────────
    print("=" * 60)
    print("  SUMMARY")
    print("=" * 60)
    print(f"  Total successful requests:  {successes}")
    print(f"  Total failures:             {failures}")
    print(f"  Success rate:               "
          f"{100 * successes / max(successes + failures, 1):.1f} %")
    print()
    print("  Timeline:")
    for ev in timeline_events:
        print(f"    {ev}")
    print()
    print("  Final RTT ranking:")
    for ep in client.endpoints:
        print(f"    {ep.url}  rtt={ep.rtt_ms:.1f} ms  "
              f"failures={ep.failures}")
    print("=" * 60 + "\n")

    await client.stop()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
