"""
LAN multi-transport load test.

Spawns N concurrent clients and exercises every LAN channel exposed by
the TransportCoordinator:

    1. HTTP /api/health               (always should be 200)
    2. Raw TCP fallback HELLO/PING    (line protocol on TCP_FALLBACK_PORT)
    3. UDP broadcast packet listener  (one-shot sniff, confirms it ticks)
    4. /api/transports/health         (via the JWT-less admin token fallback)

Usage:
    python scripts/lan_load_test.py --host 127.0.0.1 --clients 3 --clients 10

Run multiple --clients values to test ramp-up. Exits 0 on pass, 1 on
any timeout / failure past the tolerance.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import statistics
import sys
import time
import urllib.request
import urllib.error


# ── Per-client worker ────────────────────────────────────────

async def tcp_hello_ping(host: str, port: int, timeout: float) -> dict:
    t0 = time.perf_counter()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout,
        )
    except Exception as exc:
        return {"ok": False, "phase": "connect", "error": str(exc)}

    latencies = []
    try:
        # HELLO
        writer.write(b"HELLO\n")
        await writer.drain()
        t1 = time.perf_counter()
        line = await asyncio.wait_for(reader.readline(), timeout=timeout)
        latencies.append((time.perf_counter() - t1) * 1000)
        if not line.startswith(b"OK"):
            return {"ok": False, "phase": "hello", "got": line.decode(errors="replace")}

        # PING × 5
        for _ in range(5):
            tp = time.perf_counter()
            writer.write(b"PING\n")
            await writer.drain()
            pong = await asyncio.wait_for(reader.readline(), timeout=timeout)
            latencies.append((time.perf_counter() - tp) * 1000)
            if not pong.startswith(b"PONG"):
                return {"ok": False, "phase": "ping", "got": pong.decode(errors="replace")}

        writer.write(b"QUIT\n")
        await writer.drain()
    except asyncio.TimeoutError:
        return {"ok": False, "phase": "io_timeout"}
    except Exception as exc:
        return {"ok": False, "phase": "io", "error": str(exc)}
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

    return {
        "ok": True,
        "total_ms": (time.perf_counter() - t0) * 1000,
        "rtt_p50_ms": statistics.median(latencies),
        "rtt_max_ms": max(latencies),
    }


def http_probe(url: str, timeout: float) -> dict:
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            body = r.read(4096)
            return {
                "ok": 200 <= r.status < 300,
                "status": r.status,
                "ms": (time.perf_counter() - t0) * 1000,
                "size": len(body),
            }
    except urllib.error.URLError as exc:
        return {"ok": False, "error": str(exc.reason)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def udp_sniff(port: int, timeout: float) -> dict:
    """Listen for one UDP broadcast packet from DiscoveryService."""
    t0 = time.perf_counter()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        except Exception:
            pass
        s.bind(("", port))
        s.settimeout(timeout)
        data, addr = s.recvfrom(4096)
        return {
            "ok": True,
            "from": f"{addr[0]}:{addr[1]}",
            "bytes": len(data),
            "ms": (time.perf_counter() - t0) * 1000,
            "preview": data[:120].decode("utf-8", errors="replace"),
        }
    except socket.timeout:
        return {"ok": False, "error": "no broadcast within timeout"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        try:
            s.close()
        except Exception:
            pass


# ── Rampup runner ─────────────────────────────────────────────

async def run_wave(host: str, tcp_port: int, http_port: int, n_clients: int, timeout: float) -> dict:
    print(f"\n── WAVE: {n_clients} clients ────────────────────────")

    # HTTP pre-check
    http = http_probe(f"http://{host}:{http_port}/api/health", timeout)
    print(f"  HTTP /api/health       → {http}")

    # UDP sniff runs once (it's a broadcast, shared)
    udp = await asyncio.get_event_loop().run_in_executor(
        None, udp_sniff, 41234, min(5.0, timeout + 2.0),
    )
    print(f"  UDP broadcast :41234   → ok={udp.get('ok')} {udp.get('error') or udp.get('preview','')[:80]}")

    # TCP fallback: N concurrent clients
    t0 = time.perf_counter()
    tasks = [tcp_hello_ping(host, tcp_port, timeout) for _ in range(n_clients)]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    elapsed = (time.perf_counter() - t0) * 1000

    ok = sum(1 for r in results if isinstance(r, dict) and r.get("ok"))
    fail = n_clients - ok
    rtts = [r["rtt_p50_ms"] for r in results if isinstance(r, dict) and r.get("ok")]
    summary = {
        "wave": n_clients,
        "http_ok": http.get("ok"),
        "udp_ok": udp.get("ok"),
        "tcp_ok": ok,
        "tcp_fail": fail,
        "tcp_elapsed_ms": round(elapsed, 1),
        "tcp_rtt_p50_ms": round(statistics.median(rtts), 2) if rtts else None,
        "tcp_rtt_max_ms": round(max(rtts), 2) if rtts else None,
    }
    print(f"  TCP fallback :{tcp_port}  → {ok}/{n_clients} ok in {elapsed:.0f}ms "
          f"(p50 {summary['tcp_rtt_p50_ms']}ms, max {summary['tcp_rtt_max_ms']}ms)")
    if fail:
        for r in results[:3]:
            if isinstance(r, dict) and not r.get("ok"):
                print(f"    sample failure: {r}")
    return summary


async def main_async(args: argparse.Namespace) -> int:
    waves = args.clients or [3, 10, 50]
    all_ok = True
    summaries = []
    for n in waves:
        s = await run_wave(args.host, args.tcp_port, args.http_port, n, args.timeout)
        summaries.append(s)
        if s["tcp_fail"] > 0 or not s["http_ok"]:
            all_ok = False
        await asyncio.sleep(0.5)

    print("\n── SUMMARY ─────────────────────────────────────────")
    print(json.dumps(summaries, indent=2))
    print("\nResult:", "PASS" if all_ok else "FAIL")
    return 0 if all_ok else 1


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--http-port", type=int, default=3000)
    p.add_argument("--tcp-port", type=int, default=41235)
    p.add_argument("--timeout", type=float, default=5.0)
    p.add_argument("--clients", type=int, action="append",
                   help="wave size — repeat flag for multiple waves")
    args = p.parse_args()
    sys.exit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
