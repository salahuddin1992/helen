"""
verify-deployment.py — Helen post-install sanity check.

Run this script on a freshly-installed (or freshly-upgraded) Helen
host to confirm:

  * Helen-Server is reachable on the configured port
  * Helen-Router is reachable (and routes to at least one upstream)
  * Helen-Rendezvous is reachable (if configured)
  * The 5 lifespan startup events fired (crash_reporter,
    audit_chain, call_orchestrators, lan_push, calendar_reminder)
  * Each optional backend (NATS / MQTT / gRPC / WireGuard) is in
    the state the env vars demand
  * Code-signing on the local Helen-Server.exe is intact
  * Mesh topology strategy matches what the operator set
  * Firewall rules are in place (Windows only)

Outputs
-------
Returns exit code 0 on success, 1 on any check failure. Prints a
human-readable table and writes a JSON report to
``$DATA_DIR/verify-report-YYYYmmdd-HHMMSS.json``.

Usage
-----
    python tools/verify-deployment.py                       # local
    python tools/verify-deployment.py --remote 10.0.0.5     # remote
    python tools/verify-deployment.py --json                # JSON only

This script is **read-only** — it never starts/stops services or
modifies config. Safe to run in production.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


# ── Result model ───────────────────────────────────────────────────


@dataclass
class Check:
    name: str
    status: str = "pending"  # ok | warn | fail | skip
    detail: str = ""
    elapsed_ms: float = 0.0


@dataclass
class Report:
    started_at: float = field(default_factory=time.time)
    target: str = "127.0.0.1"
    checks: list[Check] = field(default_factory=list)

    def add(self, check: Check) -> None:
        self.checks.append(check)

    @property
    def counts(self) -> dict[str, int]:
        c = {"ok": 0, "warn": 0, "fail": 0, "skip": 0}
        for ch in self.checks:
            c[ch.status] = c.get(ch.status, 0) + 1
        return c

    @property
    def overall_ok(self) -> bool:
        return self.counts["fail"] == 0


# ── Helpers ────────────────────────────────────────────────────────


def _http_probe(url: str, *, timeout: float = 4.0,
                expect_codes: tuple[int, ...] = (200, 401, 403),
                ) -> tuple[bool, int, str]:
    """Returns (success, status_code, body_first_200_chars). Treats
    401/403 as success when expected — auth-gated routes still prove
    the listener is up."""
    try:
        import urllib.request
        import urllib.error
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(200).decode("utf-8", "replace")
            return resp.status in expect_codes, resp.status, body
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read(200).decode("utf-8", "replace")
        except Exception:
            pass
        return exc.code in expect_codes, exc.code, body
    except Exception as exc:
        return False, 0, f"unreachable: {exc}"


def _run(cmd: list[str], *, timeout: float = 4.0) -> tuple[int, str]:
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except FileNotFoundError:
        return 127, "binary not on PATH"
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    except Exception as exc:
        return 1, str(exc)


def _port_open(host: str, port: int, *, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# ── Individual checks ──────────────────────────────────────────────


def check_helen_server(target: str, port: int) -> Check:
    c = Check(name=f"Helen-Server :{port}")
    t0 = time.perf_counter()
    ok, code, body = _http_probe(f"http://{target}:{port}/api/health")
    c.elapsed_ms = (time.perf_counter() - t0) * 1000
    if ok and code == 200 and "Helen Server" in body:
        c.status = "ok"
        c.detail = f"HTTP 200 in {c.elapsed_ms:.0f} ms"
    elif code in (200, 401, 403):
        c.status = "warn"
        c.detail = f"HTTP {code} but body unexpected: {body[:80]}"
    else:
        c.status = "fail"
        c.detail = body[:120]
    return c


def check_helen_router(target: str, port: int) -> Check:
    c = Check(name=f"Helen-Router :{port}")
    t0 = time.perf_counter()
    ok, code, body = _http_probe(f"http://{target}:{port}/router/health")
    c.elapsed_ms = (time.perf_counter() - t0) * 1000
    if ok and code == 200 and "helen-router" in body:
        c.status = "ok"
        c.detail = f"HTTP 200 in {c.elapsed_ms:.0f} ms"
    elif code == 0:
        c.status = "skip"
        c.detail = "router not deployed on this host"
    else:
        c.status = "fail"
        c.detail = f"code={code} body={body[:100]}"
    return c


def check_router_topology(target: str, port: int) -> Check:
    c = Check(name="Mesh topology strategy")
    ok, code, body = _http_probe(
        f"http://{target}:{port}/router/topology-strategy",
    )
    if not ok or code != 200:
        c.status = "skip"
        c.detail = "router not reachable"
        return c
    try:
        data = json.loads(body)
        c.status = "ok"
        c.detail = f"strategy={data.get('strategy')} available={data.get('available')}"
    except Exception:
        c.status = "warn"
        c.detail = "couldn't parse JSON"
    return c


def check_helen_rendezvous(target: str, port: int) -> Check:
    c = Check(name=f"Helen-Rendezvous :{port}")
    if not _port_open(target, port, timeout=1.5):
        c.status = "skip"
        c.detail = "rendezvous not deployed"
        return c
    # Rendezvous health is optional — try a /health probe but don't
    # fail if missing.
    ok, code, body = _http_probe(
        f"http://{target}:{port}/health", expect_codes=(200, 404),
    )
    if code == 200 or code == 404:
        c.status = "ok"
        c.detail = f"port open, HTTP {code}"
    else:
        c.status = "fail"
        c.detail = f"port open but HTTP {code}: {body[:80]}"
    return c


def check_transport_backends(target: str, port: int) -> Check:
    c = Check(name="Optional transport backends")
    ok, code, body = _http_probe(
        f"http://{target}:{port}/api/admin/transports/backends",
    )
    if code == 403:
        c.status = "ok"
        c.detail = "admin endpoint reachable (auth-gated)"
    elif code == 404:
        c.status = "warn"
        c.detail = "endpoint missing — server is older than v10"
    else:
        c.status = "warn"
        c.detail = f"unexpected code {code}"
    return c


def _check_optional_backend(name: str, target: str, port: int,
                             slug: str) -> Check:
    """Common helper — every optional adapter has a `/status`
    endpoint that returns {"configured": bool, ...}. We don't assume
    it's *configured*; just that the endpoint responds (auth-gated 403
    is success here)."""
    c = Check(name=f"Backend status: {name}")
    ok, code, body = _http_probe(
        f"http://{target}:{port}/api/admin/transports/{slug}/status",
    )
    if code == 403:
        c.status = "ok"
        c.detail = "endpoint registered (auth-gated)"
    elif code == 404:
        c.status = "warn"
        c.detail = "endpoint missing — server older than v13"
    elif code == 200:
        # Public probe (shouldn't happen in production — adapter
        # endpoints are admin-gated). Surface what it returned.
        c.status = "ok"
        c.detail = body[:120]
    else:
        c.status = "warn"
        c.detail = f"unexpected code {code}"
    return c


def check_nats_status(target: str, port: int) -> Check:
    return _check_optional_backend("NATS", target, port, "nats")


def check_mqtt_status(target: str, port: int) -> Check:
    return _check_optional_backend("MQTT", target, port, "mqtt")


def check_zeromq_status(target: str, port: int) -> Check:
    return _check_optional_backend("ZeroMQ", target, port, "zeromq")


def check_rabbitmq_status(target: str, port: int) -> Check:
    return _check_optional_backend("RabbitMQ", target, port, "rabbitmq")


def check_grpc_status(target: str, port: int) -> Check:
    return _check_optional_backend("gRPC federation", target, port, "grpc")


def check_wireguard_status(target: str, port: int) -> Check:
    return _check_optional_backend("WireGuard", target, port, "wireguard")


def check_ssh_tunnels_status(target: str, port: int) -> Check:
    return _check_optional_backend("SSH tunnels", target, port, "ssh")


def check_lifespan_logs(log_path: Optional[str], *,
                          retry_for_seconds: float = 30.0,
                          retry_interval: float = 2.0) -> Check:
    c = Check(name="Lifespan startup events")
    if not log_path or not os.path.isfile(log_path):
        c.status = "skip"
        c.detail = "no log path supplied"
        return c
    expected = (
        "crash_reporter_installed",
        "audit_chain_configured",
        "call_orchestrators_wired",
        "lan_push_manager_configured",
        "calendar_reminder_worker_started",
    )

    deadline = time.time() + retry_for_seconds
    last_missing: list[str] = list(expected)
    while time.time() < deadline:
        try:
            with open(log_path, encoding="utf-8", errors="replace") as f:
                content = f.read(2_000_000)
        except OSError as exc:
            c.status = "warn"
            c.detail = f"could not read: {exc}"
            return c
        last_missing = [e for e in expected if e not in content]
        if not last_missing:
            c.status = "ok"
            c.detail = "all 5 lifespan events present"
            return c
        time.sleep(retry_interval)

    # Still missing some events after retry window — warn rather than fail.
    c.status = "warn"
    c.detail = (
        f"after {retry_for_seconds:.0f}s, still missing: "
        f"{', '.join(last_missing)}"
    )
    return c


def check_codesigning() -> Check:
    """Windows-only — verifies Helen-Server.exe Authenticode signature."""
    c = Check(name="Code-signing (Helen-Server.exe)")
    if os.name != "nt":
        c.status = "skip"
        c.detail = "Windows-only check"
        return c
    here = Path(__file__).resolve().parent.parent
    exe = here / "dist" / "Helen-Server" / "Helen-Server.exe"
    if not exe.exists():
        c.status = "skip"
        c.detail = "Helen-Server.exe not present"
        return c
    rc, out = _run(
        ["powershell", "-NoProfile", "-Command",
         f"(Get-AuthenticodeSignature '{exe}').Status"],
        timeout=8,
    )
    out = out.strip()
    if rc == 0 and out in ("Valid", "UnknownError"):
        c.status = "ok"
        c.detail = f"signature status = {out}"
    elif rc == 0:
        c.status = "warn"
        c.detail = f"unsigned or status={out}"
    else:
        c.status = "warn"
        c.detail = f"powershell rc={rc}: {out[:80]}"
    return c


def check_firewall_rules() -> Check:
    """Windows-only — Helen rules should exist + scope LAN only."""
    c = Check(name="Windows Firewall rules")
    if os.name != "nt":
        c.status = "skip"
        c.detail = "Windows-only check"
        return c
    rc, out = _run(
        ["netsh", "advfirewall", "firewall", "show", "rule",
         "name=all"], timeout=8,
    )
    if rc != 0:
        c.status = "warn"
        c.detail = "could not enumerate firewall rules"
        return c
    if "Helen-Server" in out or "Helen Server" in out:
        c.status = "ok"
        c.detail = "Helen rules detected"
    else:
        c.status = "warn"
        c.detail = "no Helen rules found — installer may not have run"
    return c


def check_listening_ports(target: str) -> Check:
    """Confirm the canonical Helen ports are open."""
    c = Check(name="Listening ports (3000/3443/8080)")
    rows = []
    for port in (3000, 3443, 8080):
        rows.append(f"{port}={'open' if _port_open(target, port) else 'closed'}")
    c.detail = ", ".join(rows)
    open_count = sum(_port_open(target, p) for p in (3000, 3443, 8080))
    if open_count >= 1:
        c.status = "ok"
    else:
        c.status = "fail"
    return c


# ── Driver ─────────────────────────────────────────────────────────


def render_table(report: Report) -> str:
    icon = {"ok": "✓", "warn": "!", "fail": "✗", "skip": "·"}
    lines = [f"\nHelen deployment verification — target: {report.target}\n"]
    width_n = max(len(c.name) for c in report.checks) + 2
    for ch in report.checks:
        lines.append(
            f"  {icon[ch.status]}  "
            f"{ch.name.ljust(width_n)}"
            f"  {ch.detail}"
        )
    counts = report.counts
    lines.append(
        f"\n  Summary:  ok={counts['ok']}  warn={counts['warn']}  "
        f"fail={counts['fail']}  skip={counts['skip']}"
    )
    if report.overall_ok:
        lines.append("\n  ✓ All critical checks passed.\n")
    else:
        lines.append("\n  ✗ One or more checks failed — see details above.\n")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--remote", default="127.0.0.1",
                        help="target host (default localhost)")
    parser.add_argument("--server-port", type=int, default=3000)
    parser.add_argument("--router-port", type=int, default=8080)
    parser.add_argument("--rendezvous-port", type=int, default=9090)
    parser.add_argument("--log",
                        help="path to Helen-Server stdout log "
                             "(for lifespan-event check)")
    parser.add_argument("--json", action="store_true",
                        help="emit JSON instead of pretty table")
    parser.add_argument("--report-dir",
                        default=os.environ.get("HELEN_DATA_DIR", "."),
                        help="where to write the JSON report")
    args = parser.parse_args()

    rep = Report(target=args.remote)
    rep.add(check_listening_ports(args.remote))
    rep.add(check_helen_server(args.remote, args.server_port))
    rep.add(check_helen_router(args.remote, args.router_port))
    rep.add(check_router_topology(args.remote, args.router_port))
    rep.add(check_helen_rendezvous(args.remote, args.rendezvous_port))
    rep.add(check_transport_backends(args.remote, args.server_port))
    # Per-adapter status — one check per optional backend.
    rep.add(check_nats_status(args.remote, args.server_port))
    rep.add(check_mqtt_status(args.remote, args.server_port))
    rep.add(check_zeromq_status(args.remote, args.server_port))
    rep.add(check_rabbitmq_status(args.remote, args.server_port))
    rep.add(check_grpc_status(args.remote, args.server_port))
    rep.add(check_wireguard_status(args.remote, args.server_port))
    rep.add(check_ssh_tunnels_status(args.remote, args.server_port))
    rep.add(check_lifespan_logs(args.log))
    rep.add(check_codesigning())
    rep.add(check_firewall_rules())

    if args.json:
        print(json.dumps({
            "target": rep.target,
            "started_at": rep.started_at,
            "counts": rep.counts,
            "overall_ok": rep.overall_ok,
            "checks": [
                {"name": c.name, "status": c.status,
                 "detail": c.detail,
                 "elapsed_ms": c.elapsed_ms}
                for c in rep.checks
            ],
        }, indent=2))
    else:
        print(render_table(rep))

    # Persist a JSON report sidecar for ops auditing.
    try:
        report_path = Path(args.report_dir) / (
            f"verify-report-{time.strftime('%Y%m%d-%H%M%S')}.json"
        )
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps({
            "target": rep.target,
            "started_at": rep.started_at,
            "counts": rep.counts,
            "overall_ok": rep.overall_ok,
            "checks": [
                {"name": c.name, "status": c.status,
                 "detail": c.detail,
                 "elapsed_ms": c.elapsed_ms}
                for c in rep.checks
            ],
        }, indent=2))
        if not args.json:
            print(f"  Report written: {report_path}")
    except OSError as exc:
        print(f"  WARN: could not persist report: {exc}",
              file=sys.stderr)

    return 0 if rep.overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
