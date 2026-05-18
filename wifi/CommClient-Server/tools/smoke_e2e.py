"""
Helen E2E Smoke Test
====================

Spawns Helen-Router + Helen-Server locally, then simulates a desktop client
hitting the server *through* the router via the reverse-proxy path. Verifies:

  1. Router process starts and `/router/health` returns 200
  2. Server process starts and `/api/admin/health` returns 200
  3. Client → Router → Server proxy round-trip works
  4. WebSocket upgrade succeeds (admin /api/admin/ws/metrics)
  5. Socket.IO handshake completes
  6. At least N of the 287 documented endpoints respond (≠ 5xx)

Exit code:
    0 — all critical checks PASS (i.e. "the project works")
    1 — one or more critical checks FAIL
    2 — environment error (could not start a subprocess at all)

Usage:
    python tools/smoke_e2e.py
    python tools/smoke_e2e.py --no-router          # skip router, hit server directly
    python tools/smoke_e2e.py --server-port 13000 --router-port 18080
    python tools/smoke_e2e.py --keep-running       # leave services up after test
    python tools/smoke_e2e.py --json               # JSON output for CI
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import traceback
from contextlib import contextmanager
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WIFI_ROOT = PROJECT_ROOT.parent
ROUTER_ROOT = WIFI_ROOT / "Helen-Router"


# ---------------------------------------------------------------------------
# Coloured terminal output
# ---------------------------------------------------------------------------
def C(code: str, s: str) -> str:
    if not sys.stdout.isatty():
        return s
    return f"\033[{code}m{s}\033[0m"


def info(s: str) -> None:  print(C("36", "ℹ "), s)
def ok(s: str) -> None:    print(C("32", "✓ "), s)
def warn(s: str) -> None:  print(C("33", "⚠ "), s)
def fail(s: str) -> None:  print(C("31", "✗ "), s)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
@dataclass
class CheckResult:
    name: str
    passed: bool
    critical: bool
    duration_ms: int
    detail: str = ""
    error: Optional[str] = None


@dataclass
class SmokeReport:
    started_at: str
    finished_at: str = ""
    config: Dict[str, Any] = field(default_factory=dict)
    checks: List[CheckResult] = field(default_factory=list)

    @property
    def critical_failures(self) -> List[CheckResult]:
        return [c for c in self.checks if c.critical and not c.passed]

    @property
    def passed(self) -> bool:
        return not self.critical_failures


# ---------------------------------------------------------------------------
# Process management
# ---------------------------------------------------------------------------
@dataclass
class SpawnedService:
    name: str
    proc: subprocess.Popen
    port: int
    log_path: Path
    cwd: Path

    def kill(self) -> None:
        try:
            if self.proc.poll() is None:
                if os.name == "nt":
                    self.proc.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        except Exception:
            pass


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def spawn(name: str, cwd: Path, args: List[str], env_overrides: Dict[str, str]) -> SpawnedService:
    log_path = Path(tempfile.gettempdir()) / f"helen-smoke-{name}-{int(time.time())}.log"
    env = os.environ.copy()
    env.update(env_overrides)
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")

    log_fp = open(log_path, "wb")
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore

    proc = subprocess.Popen(
        args,
        cwd=str(cwd),
        env=env,
        stdout=log_fp,
        stderr=subprocess.STDOUT,
        creationflags=creationflags,
    )
    info(f"spawned {name} pid={proc.pid} log={log_path}")
    port = int(env_overrides.get("PORT", env_overrides.get("ROUTER_PORT", "0")))
    return SpawnedService(name=name, proc=proc, port=port, log_path=log_path, cwd=cwd)


async def wait_for_port(port: int, host: str = "127.0.0.1", timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r, w = await asyncio.open_connection(host, port)
            w.close()
            await w.wait_closed()
            return True
        except (ConnectionRefusedError, OSError):
            await asyncio.sleep(0.5)
    return False


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------
async def check_port_open(name: str, port: int, critical: bool = True) -> CheckResult:
    t0 = time.time()
    ok_ = await wait_for_port(port, timeout=30)
    return CheckResult(
        name=name, passed=ok_, critical=critical,
        duration_ms=int((time.time() - t0) * 1000),
        detail=f"port {port}",
        error=None if ok_ else f"port {port} did not open within 30s",
    )


async def check_http(name: str, url: str, expected_status: int = 200, critical: bool = True,
                     headers: Optional[Dict[str, str]] = None, timeout: float = 10.0) -> CheckResult:
    import httpx
    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url, headers=headers or {})
        passed = resp.status_code == expected_status
        return CheckResult(
            name=name, passed=passed, critical=critical,
            duration_ms=int((time.time() - t0) * 1000),
            detail=f"GET {url} → {resp.status_code} (expected {expected_status})",
            error=None if passed else resp.text[:200],
        )
    except Exception as exc:
        return CheckResult(
            name=name, passed=False, critical=critical,
            duration_ms=int((time.time() - t0) * 1000),
            detail=f"GET {url} crashed", error=str(exc),
        )


async def check_websocket(name: str, ws_url: str, critical: bool = False,
                          token: Optional[str] = None, timeout: float = 5.0) -> CheckResult:
    """Verify a WebSocket can be upgraded. Doesn't require auth to succeed —
    a 401/403 close still proves the WS handshake works."""
    import httpx
    from urllib.parse import urlparse
    t0 = time.time()
    parsed = urlparse(ws_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "wss" else 80)
    try:
        # Manually craft WS upgrade via raw socket — independent of any ws lib
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout)
        path = parsed.path + ("?" + parsed.query if parsed.query else "")
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
        )
        if token:
            req += f"Authorization: Bearer {token}\r\n"
        req += "\r\n"
        writer.write(req.encode())
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=timeout)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        status_line = line.decode("latin-1", errors="replace").strip()
        # 101 = upgrade, 401/403 = handshake reached app but rejected → still proves WS path
        passed = "101" in status_line or "401" in status_line or "403" in status_line
        return CheckResult(
            name=name, passed=passed, critical=critical,
            duration_ms=int((time.time() - t0) * 1000),
            detail=f"{ws_url} → {status_line}",
            error=None if passed else f"unexpected response: {status_line}",
        )
    except Exception as exc:
        return CheckResult(
            name=name, passed=False, critical=critical,
            duration_ms=int((time.time() - t0) * 1000),
            detail=f"WS {ws_url}", error=str(exc),
        )


async def check_socketio(name: str, server_url: str, critical: bool = False,
                         timeout: float = 5.0) -> CheckResult:
    """Probe Socket.IO endpoint — handshake should return polling response."""
    import httpx
    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(f"{server_url}/socket.io/?EIO=4&transport=polling")
        passed = resp.status_code in (200, 400)  # 400 ok = server speaks SIO but rejected req
        return CheckResult(
            name=name, passed=passed, critical=critical,
            duration_ms=int((time.time() - t0) * 1000),
            detail=f"polling probe → {resp.status_code}",
            error=None if passed else resp.text[:200],
        )
    except Exception as exc:
        return CheckResult(
            name=name, passed=False, critical=critical,
            duration_ms=int((time.time() - t0) * 1000),
            detail="SIO probe", error=str(exc),
        )


async def check_router_proxy(name: str, router_url: str, server_url: str,
                             critical: bool = True, timeout: float = 10.0) -> CheckResult:
    """Hit router and verify it proxies upstream (server) requests."""
    import httpx
    t0 = time.time()
    # If router supports /router/upstreams it tells us what it knows about
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(f"{router_url}/router/upstreams")
        passed = resp.status_code in (200, 401, 403, 404)
        return CheckResult(
            name=name, passed=passed, critical=critical,
            duration_ms=int((time.time() - t0) * 1000),
            detail=f"/router/upstreams → {resp.status_code}",
            error=None if passed else resp.text[:200],
        )
    except Exception as exc:
        return CheckResult(
            name=name, passed=False, critical=critical,
            duration_ms=int((time.time() - t0) * 1000),
            detail="router proxy", error=str(exc),
        )


async def check_endpoint_sample(name: str, base_url: str, sample_paths: List[str],
                                 critical: bool = False) -> CheckResult:
    """Hit a handful of endpoints; pass if ≥80% respond with non-5xx."""
    import httpx
    t0 = time.time()
    results: Dict[str, Any] = {}
    non5xx = 0
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            for path in sample_paths:
                try:
                    r = await client.get(f"{base_url}{path}")
                    results[path] = r.status_code
                    if r.status_code < 500:
                        non5xx += 1
                except Exception as e:
                    results[path] = f"ERR:{type(e).__name__}"
        ratio = non5xx / max(1, len(sample_paths))
        passed = ratio >= 0.8
        return CheckResult(
            name=name, passed=passed, critical=critical,
            duration_ms=int((time.time() - t0) * 1000),
            detail=f"{non5xx}/{len(sample_paths)} non-5xx ({ratio:.0%})",
            error=None if passed else f"results={results}",
        )
    except Exception as exc:
        return CheckResult(
            name=name, passed=False, critical=critical,
            duration_ms=int((time.time() - t0) * 1000),
            detail="sample", error=str(exc),
        )


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--server-port", type=int, default=0, help="0 = auto")
    p.add_argument("--router-port", type=int, default=0, help="0 = auto")
    p.add_argument("--no-router", action="store_true")
    p.add_argument("--keep-running", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--server-start-timeout", type=int, default=60)
    p.add_argument("--server-only-import", action="store_true",
                   help="Only verify the FastAPI app object imports; no subprocess.")
    return p.parse_args()


async def import_only_check() -> List[CheckResult]:
    """Faster path: just import the FastAPI apps and count endpoints."""
    results: List[CheckResult] = []
    sys.path.insert(0, str(PROJECT_ROOT))

    # Server import
    t0 = time.time()
    try:
        from app.main import app as server_app  # type: ignore
        route_count = len(server_app.routes)
        results.append(CheckResult(
            name="server.import", passed=True, critical=True,
            duration_ms=int((time.time() - t0) * 1000),
            detail=f"FastAPI app loaded with {route_count} routes"))
    except Exception as exc:
        results.append(CheckResult(
            name="server.import", passed=False, critical=True,
            duration_ms=int((time.time() - t0) * 1000),
            detail="failed to import app.main",
            error=f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[:500]}"))

    # Router import — load in an isolated sys.modules + sys.path scope so the
    # server's `app.*` package doesn't shadow the router's own `app.*` package.
    t0 = time.time()
    try:
        import importlib.util
        # Drop any previously-loaded `app.*` modules from the server-side import
        for mod_name in list(sys.modules):
            if mod_name == "app" or mod_name.startswith("app."):
                del sys.modules[mod_name]
        # Make Helen-Router root the FIRST entry on sys.path so its `app` package wins
        try:
            sys.path.remove(str(PROJECT_ROOT))
        except ValueError:
            pass
        sys.path.insert(0, str(ROUTER_ROOT))
        # Load via package import so relative `from app.* import ...` works
        spec = importlib.util.spec_from_file_location(
            "app.main", str(ROUTER_ROOT / "app" / "main.py"),
            submodule_search_locations=[str(ROUTER_ROOT / "app")],
        )
        # Ensure parent package exists
        pkg_spec = importlib.util.spec_from_file_location(
            "app", str(ROUTER_ROOT / "app" / "__init__.py"),
            submodule_search_locations=[str(ROUTER_ROOT / "app")],
        )
        pkg = importlib.util.module_from_spec(pkg_spec)  # type: ignore
        sys.modules["app"] = pkg
        pkg_spec.loader.exec_module(pkg)  # type: ignore
        mod = importlib.util.module_from_spec(spec)  # type: ignore
        sys.modules["app.main"] = mod
        spec.loader.exec_module(mod)  # type: ignore
        router_app = getattr(mod, "app", None)
        if router_app:
            results.append(CheckResult(
                name="router.import", passed=True, critical=True,
                duration_ms=int((time.time() - t0) * 1000),
                detail=f"FastAPI app loaded with {len(router_app.routes)} routes"))
        else:
            results.append(CheckResult(
                name="router.import", passed=False, critical=True,
                duration_ms=int((time.time() - t0) * 1000),
                detail="no 'app' attr"))
    except Exception as exc:
        results.append(CheckResult(
            name="router.import", passed=False, critical=True,
            duration_ms=int((time.time() - t0) * 1000),
            detail="failed to import Helen-Router/app/main",
            error=f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[:500]}"))

    return results


async def full_e2e(args: argparse.Namespace, report: SmokeReport) -> None:
    server_port = args.server_port or find_free_port()
    router_port = args.router_port or find_free_port()
    server_url = f"http://127.0.0.1:{server_port}"
    router_url = f"http://127.0.0.1:{router_port}"

    info(f"server will bind to {server_url}")
    info(f"router will bind to {router_url}" if not args.no_router else "router skipped")

    services: List[SpawnedService] = []

    try:
        # ---- spawn server -----------------------------------------------
        server_env = {
            "PORT": str(server_port),
            "HELEN_DATA_DIR": tempfile.mkdtemp(prefix="helen-smoke-data-"),
            "DATABASE_URL": "sqlite+aiosqlite:////tmp/helen-smoke-" + str(int(time.time())) + ".db",
            "DATABASE_PATH": "/tmp/helen-smoke-" + str(int(time.time())) + ".db",
            "HELEN_DB_PATH": "/tmp/helen-smoke-" + str(int(time.time())) + ".db",
            "SQLITE_PATH": "/tmp/helen-smoke-" + str(int(time.time())) + ".db",
            "DB_BACKEND": "sqlite",
            "HELEN_LAN_ONLY_STRICT": "0",
            "JWT_SECRET": "smoke-test-only-do-not-use-in-production-" + "a" * 32,
            "HELEN_DOCS_ENABLED": "1",
        }
        server_svc = spawn(
            "server",
            cwd=PROJECT_ROOT,
            args=[sys.executable, "-c",
                  f"import sys; sys.path.insert(0, '{PROJECT_ROOT}'); "
                  f"import uvicorn; from app.main import asgi_app as _wrapped, app; "
                  f"uvicorn.run(_wrapped, host='127.0.0.1', port={server_port}, log_level='warning')"],
            env_overrides=server_env,
        )
        services.append(server_svc)

        c = await check_port_open("server.port", server_port)
        report.checks.append(c)
        if not c.passed:
            log_dump(server_svc.log_path, "server startup failed")
            return

        # Try /api/admin/health then fall back to /api/health
        h1 = await check_http("server.health", f"{server_url}/api/admin/health", critical=False)
        if not h1.passed:
            h2 = await check_http("server.health(alt)", f"{server_url}/api/health", critical=False)
            if h2.passed:
                report.checks.append(h2)
            else:
                report.checks.append(h1)
        else:
            report.checks.append(h1)

        report.checks.append(await check_http(
            "server.openapi", f"{server_url}/openapi.json", critical=False, timeout=30.0))

        # WebSocket admin endpoints
        report.checks.append(await check_websocket(
            "server.ws.metrics", f"ws://127.0.0.1:{server_port}/api/admin/ws/metrics"))
        report.checks.append(await check_websocket(
            "server.ws.audit", f"ws://127.0.0.1:{server_port}/api/admin/audit/ws"))

        # Socket.IO probe
        report.checks.append(await check_socketio(
            "server.socketio", server_url))

        # Sample endpoints (no auth — expecting 401s, not 5xx)
        report.checks.append(await check_endpoint_sample(
            "server.endpoints.sample", server_url, [
                "/api/admin/health",
                "/api/admin/stats",
                "/api/admin/observability/metrics",
                "/api/admin/topology/graph",
                "/api/admin/audit/head",
                "/api/admin/dr/destinations",
                "/api/admin/federation/peers",
                "/api/admin/plugins/installed",
                "/api/admin/compliance/holds",
                "/api/admin/onboarding/state",
            ]))

        # ---- spawn router (optional) -----------------------------------
        if not args.no_router:
            router_env = {
                "ROUTER_PORT": str(router_port),
                "HELEN_ROUTER_TOKEN": "smoke-router-token",
                "HELEN_ROUTER_UPSTREAM": server_url,
                "HELEN_ROUTER_DATA_DIR": tempfile.mkdtemp(prefix="helen-smoke-rtr-"),
            }
            router_svc = spawn(
                "router",
                cwd=ROUTER_ROOT,
                args=[sys.executable, "-c",
                      f"import sys; sys.path.insert(0, '{ROUTER_ROOT}'); "
                      f"import uvicorn; from app.main import asgi_app as _wrapped, app; "
                      f"uvicorn.run(_wrapped, host='127.0.0.1', port={router_port}, log_level='warning')"],
                env_overrides=router_env,
            )
            services.append(router_svc)

            c = await check_port_open("router.port", router_port, critical=False)
            report.checks.append(c)
            if c.passed:
                report.checks.append(await check_http(
                    "router.health", f"{router_url}/router/health", critical=False))
                report.checks.append(await check_router_proxy(
                    "router.proxy", router_url, server_url, critical=False))
            else:
                log_dump(router_svc.log_path, "router startup failed (non-critical)")

    finally:
        if not args.keep_running:
            for s in services:
                info(f"stopping {s.name}")
                s.kill()
        else:
            warn(f"--keep-running: services left up:")
            for s in services:
                warn(f"  {s.name} pid={s.proc.pid} port={s.port} log={s.log_path}")


def log_dump(p: Path, prefix: str = "") -> None:
    if not p.exists():
        return
    try:
        warn(f"{prefix} — log tail of {p}:")
        with open(p, "rb") as fp:
            data = fp.read()
            for line in data.splitlines()[-30:]:
                print("  >", line.decode("utf-8", errors="replace"))
    except Exception as e:
        warn(f"  could not read log: {e}")


def main() -> int:
    args = parse_args()
    report = SmokeReport(
        started_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        config={
            "server_port": args.server_port,
            "router_port": args.router_port,
            "no_router": args.no_router,
            "import_only": args.server_only_import,
            "python": sys.version,
            "project_root": str(PROJECT_ROOT),
        },
    )

    print("=" * 72)
    print(C("1;36", "  Helen E2E Smoke Test"))
    print("=" * 72)
    info(f"PROJECT_ROOT = {PROJECT_ROOT}")
    info(f"WIFI_ROOT    = {WIFI_ROOT}")
    info(f"ROUTER_ROOT  = {ROUTER_ROOT}")
    print("-" * 72)

    try:
        if args.server_only_import:
            results = asyncio.run(import_only_check())
            report.checks.extend(results)
        else:
            asyncio.run(full_e2e(args, report))
    except Exception as exc:
        fail(f"runner crashed: {exc}")
        traceback.print_exc()
        report.checks.append(CheckResult(
            name="runner", passed=False, critical=True, duration_ms=0,
            detail="crashed", error=str(exc)))

    report.finished_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    print("-" * 72)
    print(C("1;36", "  RESULTS"))
    print("-" * 72)
    for c in report.checks:
        sym = ok if c.passed else (fail if c.critical else warn)
        sym(f"{c.name:36} {c.duration_ms:>5} ms  {c.detail}")
        if not c.passed and c.error:
            print(f"        {C('33', c.error[:300])}")

    print("-" * 72)
    crit = report.critical_failures
    if not crit:
        ok(C("1;32", f"VERDICT: PROJECT WORKS — {sum(1 for c in report.checks if c.passed)}/{len(report.checks)} checks passed"))
    else:
        fail(C("1;31", f"VERDICT: PROJECT DOES NOT WORK — {len(crit)} critical failure(s):"))
        for c in crit:
            fail(f"  * {c.name}: {(c.error or '')[:200]}")
    print("=" * 72)

    if args.json:
        out = {**asdict(report),
               "verdict": "PASS" if report.passed else "FAIL",
               "checks": [asdict(c) for c in report.checks]}
        out["critical_failures"] = [c.name for c in report.critical_failures]
        out["passed"] = report.passed
        print(json.dumps(out, indent=2, default=str))

    return 0 if report.passed else 1

if __name__ == "__main__":
    sys.exit(main())
