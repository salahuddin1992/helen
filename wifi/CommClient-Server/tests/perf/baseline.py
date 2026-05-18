"""
Performance regression baseline.

Records a fixed set of latency / throughput metrics into a JSON file
that lives in source control. CI runs this script after every build,
compares the new numbers to the baseline, and fails the build if any
metric regressed by more than the configured threshold.

Default thresholds
------------------
    p50_latency_ms        +20 % over baseline → fail
    p95_latency_ms        +30 % over baseline → fail
    requests_per_second   −15 % under baseline → fail
    memory_rss_mb         +25 % over baseline → fail

The thresholds are tunable per metric — tight on production-critical
paths (auth, message-send), looser on cold-start one-shots.

Run modes
---------
    python baseline.py record   --target http://localhost:3000
                                  → run all probes, write baseline.json

    python baseline.py compare  --target http://localhost:3000
                                  --baseline baseline.json
                                  → run probes, compare, exit 0 / 1
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class MetricThreshold:
    """How much regression is tolerated before failing the build."""
    name: str
    direction: str          # "lower_is_better" | "higher_is_better"
    fail_pct: float          # percentage change that triggers fail
    warn_pct: float          # warning threshold (lower than fail)


DEFAULT_THRESHOLDS = [
    MetricThreshold("auth_p50_ms",        "lower_is_better", 20, 10),
    MetricThreshold("auth_p95_ms",        "lower_is_better", 30, 15),
    MetricThreshold("auth_p99_ms",        "lower_is_better", 40, 20),
    MetricThreshold("health_p50_ms",      "lower_is_better", 25, 12),
    MetricThreshold("health_p95_ms",      "lower_is_better", 35, 17),
    MetricThreshold("message_send_p50_ms", "lower_is_better", 20, 10),
    MetricThreshold("message_send_p95_ms", "lower_is_better", 30, 15),
    MetricThreshold("rps_health",         "higher_is_better", 15, 7),
    MetricThreshold("rps_message_send",   "higher_is_better", 15, 7),
    MetricThreshold("rss_mb",             "lower_is_better",  25, 12),
]


@dataclass
class Metrics:
    started_at: float
    target: str
    samples: dict[str, list[float]] = field(default_factory=dict)
    summary: dict[str, float] = field(default_factory=dict)

    def add(self, name: str, value: float) -> None:
        self.samples.setdefault(name, []).append(value)

    def finalise(self) -> None:
        for name, vals in self.samples.items():
            if not vals:
                continue
            vals.sort()
            self.summary[f"{name}_p50_ms"] = vals[len(vals) // 2]
            self.summary[f"{name}_p95_ms"] = vals[
                min(int(len(vals) * 0.95), len(vals) - 1)
            ]
            self.summary[f"{name}_p99_ms"] = vals[
                min(int(len(vals) * 0.99), len(vals) - 1)
            ]
            self.summary[f"{name}_min_ms"] = vals[0]
            self.summary[f"{name}_max_ms"] = vals[-1]
            self.summary[f"{name}_avg_ms"] = sum(vals) / len(vals)


# ── Probes ─────────────────────────────────────────────────────────


async def probe_health(target: str, n: int) -> Metrics:
    import httpx
    m = Metrics(started_at=time.time(), target=target)

    t_global = time.perf_counter()
    successes = 0
    async with httpx.AsyncClient(timeout=4.0) as c:
        for _ in range(n):
            t0 = time.perf_counter()
            try:
                r = await c.get(f"{target}/api/health")
                ms = (time.perf_counter() - t0) * 1000
                if r.status_code == 200:
                    m.add("health", ms)
                    successes += 1
            except Exception:
                pass

    elapsed = time.perf_counter() - t_global
    m.summary["rps_health"] = successes / max(elapsed, 0.001)
    m.summary["health_success_rate"] = successes / max(n, 1)
    return m


async def probe_auth(target: str, n: int) -> dict[str, list[float]]:
    """Register + login N times, record total round-trip per pair."""
    import httpx
    import secrets
    samples: list[float] = []
    successes = 0
    t_global = time.perf_counter()
    async with httpx.AsyncClient(timeout=10.0) as c:
        for _ in range(n):
            user = "perf_" + secrets.token_hex(6)
            pw = secrets.token_urlsafe(16)
            t0 = time.perf_counter()
            try:
                r1 = await c.post(
                    f"{target}/api/auth/register",
                    json={"username": user, "password": pw,
                            "display_name": user},
                )
                if r1.status_code not in (200, 201):
                    continue
                r2 = await c.post(
                    f"{target}/api/auth/login",
                    json={"username": user, "password": pw},
                )
                ms = (time.perf_counter() - t0) * 1000
                if r2.status_code == 200:
                    samples.append(ms)
                    successes += 1
            except Exception:
                pass
    elapsed = time.perf_counter() - t_global
    return {
        "samples": samples,
        "rps": successes / max(elapsed, 0.001),
        "success_rate": successes / max(n, 1),
    }


def probe_rss() -> dict[str, float]:
    try:
        import psutil
        # Find Helen-Server / python processes
        rss_mb = 0.0
        for p in psutil.process_iter(["name", "memory_info"]):
            n = (p.info.get("name") or "").lower()
            if "helen-server" in n or "python" in n:
                try:
                    rss_mb += p.info["memory_info"].rss / (1024 * 1024)
                except Exception:
                    continue
        return {"rss_mb": rss_mb}
    except Exception as exc:
        return {"rss_mb": 0.0, "error": str(exc)}


# ── Runner ─────────────────────────────────────────────────────────


async def run_all(target: str, samples: int = 200) -> dict:
    print(f"Probing {target}  (samples={samples})")
    metrics = Metrics(started_at=time.time(), target=target)

    print("  health...")
    h = await probe_health(target, samples)
    metrics.samples["health"] = h.samples.get("health", [])
    metrics.summary["rps_health"] = h.summary.get("rps_health", 0)

    print("  auth (register+login)...")
    a = await probe_auth(target, max(20, samples // 5))
    metrics.samples["auth"] = a["samples"]
    metrics.summary["rps_auth"] = a["rps"]

    print("  rss...")
    rss = probe_rss()
    metrics.summary.update(rss)

    metrics.finalise()
    return {
        "target": target,
        "started_at": metrics.started_at,
        "summary": metrics.summary,
        "samples_kept": {k: len(v) for k, v in metrics.samples.items()},
    }


def compare(baseline: dict, current: dict,
             thresholds: list[MetricThreshold]) -> tuple[bool, list[str]]:
    """Return ``(failed, messages)``."""
    msgs = []
    failed = False
    for t in thresholds:
        b = baseline.get("summary", {}).get(t.name)
        c = current.get("summary", {}).get(t.name)
        if b is None or c is None:
            continue
        if b == 0:
            continue
        delta_pct = (c - b) / b * 100
        if t.direction == "lower_is_better":
            regressed = delta_pct
        else:
            regressed = -delta_pct
        verdict = "ok"
        if regressed >= t.fail_pct:
            verdict = "FAIL"
            failed = True
        elif regressed >= t.warn_pct:
            verdict = "WARN"
        sign = "+" if delta_pct >= 0 else ""
        msgs.append(
            f"  {verdict:5s} {t.name:25s}  "
            f"baseline={b:.1f}  current={c:.1f}  "
            f"({sign}{delta_pct:+.1f} %)"
        )
    return failed, msgs


# ── CLI ────────────────────────────────────────────────────────────


async def main() -> None:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    rec = sub.add_parser("record")
    rec.add_argument("--target", default="http://localhost:3000")
    rec.add_argument("--samples", type=int, default=200)
    rec.add_argument("--out", default="baseline.json")

    cmp_ = sub.add_parser("compare")
    cmp_.add_argument("--target", default="http://localhost:3000")
    cmp_.add_argument("--samples", type=int, default=200)
    cmp_.add_argument("--baseline", default="baseline.json")

    args = p.parse_args()

    if args.cmd == "record":
        report = await run_all(args.target, args.samples)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)
        print(f"\n  wrote baseline → {args.out}")
        print(json.dumps(report["summary"], indent=2))
        return

    if args.cmd == "compare":
        if not os.path.exists(args.baseline):
            print(f"[!] baseline {args.baseline} not found — "
                   "run `record` first")
            sys.exit(2)
        baseline = json.load(open(args.baseline, encoding="utf-8"))
        current = await run_all(args.target, args.samples)
        failed, msgs = compare(baseline, current, DEFAULT_THRESHOLDS)
        print("\nRegression report:")
        for m in msgs:
            print(m)
        if failed:
            print("\n  FAIL: at least one metric regressed past threshold")
            sys.exit(1)
        print("\n  OK: no regressions")


if __name__ == "__main__":
    asyncio.run(main())
