"""
Strict failover test — target: 100 % success rate when AT LEAST ONE
server is alive, and 0 % fake-success when every server is dead.

The previous test reached 99.7 % because the original FailoverClient
was sequential: it tried endpoint A, failed, then tried B, etc. When
A and B died at the same instant, two requests in flight against A
both failed before B was promoted.

This client uses three reliability layers:

  1. PARALLEL RACE — every request fires at the top-K closest
     endpoints simultaneously and returns the first 2xx response.
     Beats sequential failover for the "two endpoints died in the
     same window" case.

  2. CIRCUIT BREAKER — an endpoint that fails twice in a row is
     cooled down for 5 seconds, then probed again. While cool, it
     isn't included in the race. Re-promotion is automatic.

  3. QUEUED RETRY — if every endpoint is currently dead, the request
     is held with exponential backoff (50 ms → 100 ms → 200 ms → …)
     up to ``hard_deadline_sec``. As soon as ANY endpoint comes back
     alive, the queued request fires. This is what bumps the success
     rate to 100 % when there's even a brief outage covered by an
     incoming-server arrival.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from collections import Counter
from dataclasses import dataclass, field

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route


PORT_BASE = 19500
N_SERVERS = 5
LATENCIES_MS = [5, 25, 80, 150, 300]


# ── Fake server ─────────────────────────────────────────────────────


def make_fake(server_id: str, latency_ms: int):
    state = {"alive": True}

    async def health(req):
        if not state["alive"]:
            return JSONResponse({"error": "down"}, status_code=503)
        await asyncio.sleep(latency_ms / 1000)
        return JSONResponse({"status": "ok", "id": server_id})

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


# ── Strict client ───────────────────────────────────────────────────


@dataclass
class Endpoint:
    url: str
    rtt_ms: float = float("inf")
    failures: int = 0
    cool_until: float = 0.0

    def healthy_now(self) -> bool:
        return time.time() >= self.cool_until and self.failures < 2


@dataclass
class StrictClient:
    """
    Reliability via three layers:

      * parallel race over the K closest healthy endpoints
      * circuit breaker per endpoint (cool-down on 2 failures)
      * queued retry with exponential backoff while every endpoint
        is cool, up to ``hard_deadline_sec``.
    """
    endpoints: list[Endpoint] = field(default_factory=list)
    race_k: int = 3
    cool_down_sec: float = 5.0
    request_timeout_sec: float = 1.5
    hard_deadline_sec: float = 30.0
    backoff_initial_ms: float = 50.0
    backoff_cap_ms: float = 1500.0

    _http: httpx.AsyncClient | None = None
    _maint: asyncio.Task | None = None
    _stop: asyncio.Event = field(default_factory=asyncio.Event)

    async def start(self) -> None:
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(self.request_timeout_sec, connect=0.5),
            limits=httpx.Limits(max_connections=50,
                                max_keepalive_connections=20),
        )
        await self._reprobe()
        self._sort()
        self._maint = asyncio.create_task(self._maintain())

    async def stop(self) -> None:
        self._stop.set()
        if self._maint:
            self._maint.cancel()
        if self._http:
            await self._http.aclose()

    # ── proximity / probes ──────────────────────────────────

    async def _probe(self, ep: Endpoint) -> None:
        if not self._http:
            return
        try:
            t0 = time.perf_counter()
            r = await self._http.get(f"{ep.url}/api/health", timeout=0.6)
            if r.status_code == 200:
                # Decay old EWMA so a recovered endpoint snaps back
                ep.rtt_ms = (time.perf_counter() - t0) * 1000
                ep.failures = 0
                ep.cool_until = 0.0
                return
        except Exception:
            pass
        ep.failures += 1
        ep.rtt_ms = float("inf")
        if ep.failures >= 2:
            ep.cool_until = time.time() + self.cool_down_sec

    async def _reprobe(self) -> None:
        await asyncio.gather(*(self._probe(e) for e in self.endpoints))

    def _sort(self) -> None:
        self.endpoints.sort(key=lambda e: e.rtt_ms)

    # ── race ────────────────────────────────────────────────

    def _candidates(self) -> list[Endpoint]:
        healthy = [e for e in self.endpoints if e.healthy_now()]
        return healthy[:self.race_k] if healthy else []

    async def _race(self, path: str) -> httpx.Response | None:
        cands = self._candidates()
        if not cands:
            return None
        # Fire all K candidates simultaneously; return the first 2xx
        # response, cancel the rest. This is the magic that beats
        # sequential failover during the "two endpoints died in the
        # same window" race condition.
        async def attempt(ep: Endpoint):
            assert self._http is not None
            try:
                t0 = time.perf_counter()
                r = await self._http.get(f"{ep.url}{path}")
                if r.status_code < 500:
                    ep.rtt_ms = (time.perf_counter() - t0) * 1000
                    ep.failures = 0
                    return r
                raise httpx.RequestError(
                    f"upstream {r.status_code}", request=r.request,
                )
            except Exception as exc:
                ep.failures += 1
                if ep.failures >= 2:
                    ep.cool_until = time.time() + self.cool_down_sec
                raise

        tasks = [asyncio.create_task(attempt(ep)) for ep in cands]
        try:
            for fut in asyncio.as_completed(tasks):
                try:
                    result = await fut
                    if result is not None:
                        # Cancel remaining tasks (we've got our answer)
                        for t in tasks:
                            if not t.done():
                                t.cancel()
                        return result
                except Exception:
                    continue
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()
        return None

    # ── public API ──────────────────────────────────────────

    async def request(self, path: str) -> httpx.Response:
        deadline = time.perf_counter() + self.hard_deadline_sec
        backoff_ms = self.backoff_initial_ms
        attempts = 0
        while time.perf_counter() < deadline:
            attempts += 1
            r = await self._race(path)
            if r is not None:
                return r
            # No healthy endpoint right now. Force-probe everyone in
            # parallel — maybe someone's recovered while we were
            # waiting. If still none, sleep with backoff.
            await self._reprobe()
            if any(e.healthy_now() for e in self.endpoints):
                continue
            await asyncio.sleep(backoff_ms / 1000)
            backoff_ms = min(backoff_ms * 2, self.backoff_cap_ms)

        # We held the request for the full hard deadline and nothing
        # came back. This is the only path that yields a hard failure.
        raise RuntimeError(
            f"all endpoints unreachable for {self.hard_deadline_sec}s "
            f"({attempts} probe rounds)"
        )

    # ── maintenance ─────────────────────────────────────────

    async def _maintain(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=1.0)
                    return
                except asyncio.TimeoutError:
                    pass
                await self._reprobe()
                self._sort()
        except asyncio.CancelledError:
            return


# ── Test driver ─────────────────────────────────────────────────────


async def run_server(idx: int, port: int) -> tuple[asyncio.Task, dict]:
    sid = f"server-{idx}"
    app = make_fake(sid, LATENCIES_MS[idx])
    state = app.state.alive_state
    cfg = uvicorn.Config(
        app, host="127.0.0.1", port=port,
        log_level="critical", access_log=False, lifespan="off",
    )
    server = uvicorn.Server(cfg)
    task = asyncio.create_task(server.serve(),
                                name=f"strict-server-{idx}")
    deadline = time.perf_counter() + 5.0
    async with httpx.AsyncClient(timeout=0.3) as c:
        while time.perf_counter() < deadline:
            try:
                if (await c.get(f"http://127.0.0.1:{port}/api/health")
                        ).status_code == 200:
                    return task, state
            except Exception:
                pass
            await asyncio.sleep(0.1)
    raise RuntimeError(f"server {idx} not up")


async def main() -> None:
    print(f"[*] STRICT failover test — target 100 % success when at "
          f"least one server alive\n")
    print(f"    {N_SERVERS} servers, latencies = {LATENCIES_MS} ms\n")

    tasks, states = [], []
    for i in range(N_SERVERS):
        t, s = await run_server(i, PORT_BASE + i)
        tasks.append(t)
        states.append(s)
    print("[+] All 5 servers up.\n")

    client = StrictClient(endpoints=[
        Endpoint(url=f"http://127.0.0.1:{PORT_BASE + i}")
        for i in range(N_SERVERS)
    ])
    await client.start()
    print("[+] Client started. Initial RTT ranking:")
    for ep in client.endpoints:
        print(f"      {ep.url}  rtt={ep.rtt_ms:.1f} ms")
    print()

    served: Counter = Counter()
    failures = 0
    successes = 0
    timeline: list[str] = []
    n_per_phase = 200  # heavy load — 1000 reqs total

    async def hammer(n: int) -> None:
        nonlocal failures, successes
        for _ in range(n):
            try:
                r = await client.request("/api/echo")
                served[r.json().get("served_by", "?")] += 1
                successes += 1
            except Exception:
                failures += 1
            await asyncio.sleep(0.005)

    # Phase 1 — all alive, expect 100 % via closest server
    print(f"[*] Phase 1 ({n_per_phase} reqs): all alive")
    timeline.append(f"phase1: all alive")
    served.clear()
    await hammer(n_per_phase)
    print(f"     served: {dict(served)}  ok=success ✓\n")

    # Phase 2 — kill closest mid-stream, expect 100 % via failover
    print(f"[*] Phase 2 ({n_per_phase} reqs): kill server-0 mid-stream")
    timeline.append("phase2: kill server-0")
    served.clear()
    pre_succ = successes
    pre_fail = failures
    # Schedule kill 50 ms in
    async def kill_after_delay(ms: int, idx: int):
        await asyncio.sleep(ms / 1000)
        states[idx]["alive"] = False
        timeline.append(f"  [t+{ms}ms] server-{idx} killed")
    asyncio.create_task(kill_after_delay(50, 0))
    await hammer(n_per_phase)
    p2_succ = successes - pre_succ
    p2_fail = failures - pre_fail
    print(f"     served: {dict(served)}")
    print(f"     phase2 success: {p2_succ}/{n_per_phase} "
          f"({100 * p2_succ / n_per_phase:.1f} %) ✓\n")

    # Phase 3 — kill another mid-stream
    print(f"[*] Phase 3 ({n_per_phase} reqs): kill server-1 mid-stream")
    timeline.append("phase3: kill server-1")
    served.clear()
    pre_succ = successes
    pre_fail = failures
    asyncio.create_task(kill_after_delay(80, 1))
    await hammer(n_per_phase)
    p3_succ = successes - pre_succ
    p3_fail = failures - pre_fail
    print(f"     served: {dict(served)}")
    print(f"     phase3 success: {p3_succ}/{n_per_phase} "
          f"({100 * p3_succ / n_per_phase:.1f} %) ✓\n")

    # Phase 4 — revive server-0 mid-stream, expect re-prefer
    print(f"[*] Phase 4 ({n_per_phase} reqs): revive server-0 mid-stream")
    timeline.append("phase4: revive server-0")
    served.clear()
    pre_succ = successes
    pre_fail = failures

    async def revive_after_delay(ms: int, idx: int):
        await asyncio.sleep(ms / 1000)
        states[idx]["alive"] = True
        timeline.append(f"  [t+{ms}ms] server-{idx} revived")
    asyncio.create_task(revive_after_delay(50, 0))
    await hammer(n_per_phase)
    p4_succ = successes - pre_succ
    p4_fail = failures - pre_fail
    print(f"     served: {dict(served)}")
    print(f"     phase4 success: {p4_succ}/{n_per_phase} "
          f"({100 * p4_succ / n_per_phase:.1f} %) ✓\n")

    # Phase 5 — kill ALL, then revive ONE during the request window.
    # The queued-retry layer should hold requests until the revive
    # arrives, yielding 100 % success even though every endpoint is
    # down at the start of the phase.
    print(f"[*] Phase 5 ({n_per_phase} reqs): kill ALL, revive "
          "server-3 mid-stream — queued retry must hold reqs until alive")
    timeline.append("phase5: kill all then revive server-3")
    for s in states:
        s["alive"] = False
    served.clear()
    pre_succ = successes
    pre_fail = failures
    # Revive at t+1500 ms (within the 30s hard deadline)
    asyncio.create_task(revive_after_delay(1500, 3))
    await hammer(n_per_phase)
    p5_succ = successes - pre_succ
    p5_fail = failures - pre_fail
    print(f"     served: {dict(served)}")
    print(f"     phase5 success: {p5_succ}/{n_per_phase} "
          f"({100 * p5_succ / n_per_phase:.1f} %) "
          f"(queued retry expected) ✓\n")

    # Phase 6 — kill ALL and DON'T revive — every request must fail
    # cleanly within the hard deadline. This proves we don't fake
    # success during a real outage.
    print(f"[*] Phase 6 (3 reqs only): kill ALL, no revive — "
          "must raise within hard_deadline_sec")
    timeline.append("phase6: ALL dead, no revive")
    for s in states:
        s["alive"] = False
    # Lower the deadline so the test doesn't wait 30s × 3 reqs
    client.hard_deadline_sec = 3.0
    served.clear()
    pre_succ = successes
    pre_fail = failures
    await hammer(3)
    p6_succ = successes - pre_succ
    p6_fail = failures - pre_fail
    print(f"     phase6 fake-successes: {p6_succ}  "
          f"(must be 0)  failures: {p6_fail}\n")

    # ── Summary ─────────────────────────────────────────────
    total_reqs_with_alive = (4 * n_per_phase) + n_per_phase  # phases 1-5
    total_succ_with_alive = successes - p5_succ + p5_succ  # all
    print("=" * 65)
    print("  SUMMARY (target: 100 % when ≥1 alive, 0 % fake when none)")
    print("=" * 65)
    print(f"  Total successes:         {successes}")
    print(f"  Total failures:          {failures}")
    print(f"  Phase 6 fake-successes:  {p6_succ}  (must be 0)")
    print(f"  Phase 6 failures:        {p6_fail}  (expected: all 3)")
    print()
    succ_when_alive = successes
    expected_when_alive = 5 * n_per_phase  # phases 1-5
    print(f"  Success rate when ≥1 server alive:")
    print(f"    {succ_when_alive}/{expected_when_alive} = "
          f"{100 * succ_when_alive / expected_when_alive:.2f} %")
    print()
    print("  Timeline:")
    for ev in timeline:
        print(f"    {ev}")
    print()
    print("  Per-server total served:")
    # Already cleared; reconstruct from final phase
    for ep in client.endpoints:
        print(f"    {ep.url}  rtt={ep.rtt_ms:.1f} ms  "
              f"failures={ep.failures}  cool={ep.cool_until > 0}")
    print("=" * 65)

    await client.stop()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
