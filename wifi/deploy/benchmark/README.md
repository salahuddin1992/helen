# Helen LAN Benchmark

Honest performance numbers for a Helen-Server deployment, measured from
a client machine on the same LAN.

## Prerequisites

```bash
pip install httpx
```

(Optional, for full WebRTC quality numbers add `aiortc` + `numpy`.)

## Run

```bash
# Quick smoke (~30 seconds total)
python3 bench.py --url http://10.0.0.5:3000

# Heavy run (~5 minutes, 1000 concurrent, 5-min sustained)
python3 bench.py --url http://10.0.0.5:3000 \
  --duration 300 --concurrent 1000 --samples 500

# Skip slow phases (e.g. when iterating)
python3 bench.py --skip auth,throughput
```

## What it measures

| Phase | What | Why |
|---|---|---|
| HTTP latency | `/api/health` p50/p95/p99 | Baseline — network + reverse-proxy overhead |
| Auth round-trip | register + login | Token gen + bcrypt cost |
| Concurrent connections | N parallel `/api/health` | Connection-pool ceiling |
| Sustained throughput | RPS over D seconds | Steady-state capacity |
| WebSocket handshake | Socket.IO polling | Real-time path warm-up time |

## What "good" looks like on a typical LAN

These are observed on a 4-core/8GB Linux VM with Helen-Server v1.0.0 on a
gigabit LAN. Your numbers will vary.

| Metric | Good | Acceptable | Investigate |
|---|---|---|---|
| HTTP p50 | < 5 ms | < 25 ms | > 100 ms |
| HTTP p95 | < 20 ms | < 100 ms | > 500 ms |
| Auth p50 | < 200 ms | < 500 ms | > 2 s |
| Concurrent (100) | 100/100 ok | 95+/100 | < 90/100 |
| Sustained RPS | 2000+ | 500+ | < 100 |
| WS handshake p95 | < 50 ms | < 200 ms | > 500 ms |

## Output

A JSON file (default `bench-results.json`) suitable for CI-style trend
tracking. Sample:

```json
{
  "target": "http://10.0.0.5:3000",
  "started_at": "2026-05-04 18:00:00",
  "http_latency": { "p50_ms": 1.8, "p95_ms": 4.2, "p99_ms": 9.1, ... },
  "auth_roundtrip": { "p50_ms": 145, "p95_ms": 320, ... },
  "concurrent_connections": { "successes": 100, "failures": 0, "rps": 1247 },
  "sustained_throughput": { "rps_avg": 2103, "successful_requests": 63090 },
  "websocket_handshake": { "p50_ms": 18, "p95_ms": 42 }
}
```

## Caveats

- Run from a **separate** machine, not on the server itself — local-only
  numbers are misleadingly fast (no network in the path).
- Don't run during a backup window.
- Auth tests create real users named `bench_<hex>`; clean them up
  periodically via `/api/admin/cleanup/test-users` if you run frequently.
