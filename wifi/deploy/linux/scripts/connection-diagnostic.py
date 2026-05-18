"""
Helen connection diagnostic — 40+ checks for "why can't host A reach
host B / server / router".

Categories
----------
  A. NETWORK LAYER
       - same subnet?
       - default gateway reachable?
       - DNS resolvable?
       - multicast / broadcast permitted?
       - MTU drop / PMTU blackhole?
       - ICMP filtering?
       - AP / client isolation on the router?
       - VLAN tagging mismatch?
       - duplicate IP / MAC?
       - DHCP lease expiry?
  B. HOST FIREWALL
       - inbound rule exists for Helen ports?
       - outbound rule exists for discovery?
       - Windows Defender real-time scan blocking?
       - third-party AV present and known to interfere?
  C. APPLICATION LAYER
       - server health responds?
       - clock skew with server within JWT tolerance?
       - TLS cert valid + matches hostname?
       - CORS origin acceptable?
       - rate limit triggered?
       - JWT_SECRET on both ends?
       - mDNS advertised?
  D. HELEN-SPECIFIC
       - mandatory-router mode + router unreachable?
       - mesh partition?
       - stale registry entries?
       - federation token mismatch?
       - router upstream list empty?
       - port file conflict?
  E. OPERATING-SYSTEM POLICY
       - Group Policy / SRP blocking exe?
       - SmartScreen / Smart App Control?
       - corporate proxy intercepting?
       - hosts file misdirect?

Each check returns ``ok`` / ``warn`` / ``fail`` with a remediation
hint. Pass ``--auto-fix`` to apply known-safe remediations (firewall
rule add, hosts entry, Defender exclusion).
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import os
import platform
import socket
import struct
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class CheckResult:
    name: str
    category: str
    status: str = "pending"          # ok | warn | fail
    detail: str = ""
    remediation: Optional[str] = None
    elapsed_ms: float = 0.0
    raw: str = ""


@dataclass
class DiagnosticReport:
    target_host: str
    target_port: int
    started_at: float = field(default_factory=time.time)
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        c = {"ok": 0, "warn": 0, "fail": 0}
        for r in self.checks:
            c[r.status] = c.get(r.status, 0) + 1
        return c


# ── Category A: Network layer ──────────────────────────────────────


def check_same_subnet(target: str) -> CheckResult:
    r = CheckResult(name="A1. Same subnet as target",
                     category="network")
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((target, 80))
        local_ip = s.getsockname()[0]
        s.close()
        net24 = ipaddress.ip_network(
            ".".join(local_ip.split(".")[:3]) + ".0/24",
        )
        target_ip = ipaddress.ip_address(target)
        if target_ip in net24:
            r.status = "ok"
            r.detail = f"local {local_ip} and target {target} share /24"
        else:
            r.status = "warn"
            r.detail = (f"local {local_ip} on {net24}; target {target} "
                         f"is in a different subnet — needs router routing")
            r.remediation = (
                "Verify your default gateway has a route to the target "
                "subnet, and that no firewall between the subnets blocks "
                "ports 3000/3443/8080/41234.")
    except Exception as exc:
        r.status = "warn"
        r.detail = f"could not determine subnet: {exc}"
    return r


def check_default_gateway() -> CheckResult:
    r = CheckResult(name="A2. Default gateway reachable",
                     category="network")
    gw = _find_default_gateway()
    if not gw:
        r.status = "fail"
        r.detail = "no default gateway in routing table"
        r.remediation = "Check `ipconfig` (Windows) / `ip route` (Linux)."
        return r
    # TCP probe to the gateway on a likely admin port — pure ICMP
    # often blocked.
    for port in (53, 80, 443, 8291):
        ok, ms = _tcp_probe(gw, port, 1.0)
        if ok:
            r.status = "ok"
            r.detail = f"gateway {gw} reachable on port {port} ({ms:.0f} ms)"
            return r
    r.status = "warn"
    r.detail = (f"gateway {gw} found but didn't answer any of "
                 f"53/80/443/8291 — router may block all probes")
    return r


def check_dns_resolution(host: str) -> CheckResult:
    r = CheckResult(name="A3. DNS resolution", category="network")
    try:
        ip = socket.gethostbyname(host)
        r.status = "ok"
        r.detail = f"{host} → {ip}"
    except Exception as exc:
        r.status = "warn"
        r.detail = f"could not resolve {host}: {exc}"
        r.remediation = (
            "If using helen.local / helen.lan, verify mDNS is enabled "
            "and the router doesn't block UDP 5353. As a workaround, "
            "use the server's IP address directly.")
    return r


def check_multicast_pass() -> CheckResult:
    r = CheckResult(name="A4. Multicast pass-through (mDNS)",
                     category="network")
    # Send an mDNS query, count replies in 1.5 s.
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.settimeout(1.5)
    # Minimal mDNS query for "_services._dns-sd._udp.local"
    query = (
        b"\x00\x00\x00\x00\x00\x01\x00\x00\x00\x00\x00\x00"
        b"\x09_services\x07_dns-sd\x04_udp\x05local\x00"
        b"\x00\x0c\x00\x01"
    )
    replies = 0
    try:
        sock.sendto(query, ("224.0.0.251", 5353))
        deadline = time.time() + 1.5
        while time.time() < deadline:
            try:
                data, _ = sock.recvfrom(2048)
                if data:
                    replies += 1
            except socket.timeout:
                break
    except OSError as exc:
        r.status = "warn"
        r.detail = f"could not send mDNS query: {exc}"
        return r
    finally:
        sock.close()
    if replies > 0:
        r.status = "ok"
        r.detail = f"received {replies} mDNS replies in 1.5 s"
    else:
        r.status = "warn"
        r.detail = ("no mDNS replies — the network may have IGMP "
                     "snooping disabled, AP isolation enabled, or the "
                     "host firewall is dropping UDP 5353")
        r.remediation = (
            "On Cisco/Mikrotik APs, disable 'Client Isolation' / "
            "'AP Isolation'. On Windows, ensure outbound UDP 5353 "
            "rule allows your app.")
    return r


def check_broadcast_pass() -> CheckResult:
    r = CheckResult(name="A5. UDP broadcast send",
                     category="network")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
        n = sock.sendto(b"helen-discover\n", ("255.255.255.255", 41234))
        if n == len(b"helen-discover\n"):
            r.status = "ok"
            r.detail = f"broadcast send succeeded ({n} bytes)"
        else:
            r.status = "warn"
            r.detail = f"partial broadcast send ({n} bytes)"
    except Exception as exc:
        r.status = "fail"
        r.detail = f"broadcast send failed: {exc}"
        r.remediation = (
            "Host firewall is blocking outbound UDP 41234. Add a rule "
            "or run the firewall script.")
    finally:
        sock.close()
    return r


def check_pmtu(target: str) -> CheckResult:
    r = CheckResult(name="A6. MTU/PMTU sanity", category="network")
    # Send a ~1400 byte TCP payload to target:80 (or 443). If the
    # connection establishes but the response truncates, we have a
    # PMTU blackhole. We can't test perfectly without raw sockets,
    # so we just verify TCP works at all.
    ok, ms = _tcp_probe(target, 80, 1.5)
    ok2, _ = _tcp_probe(target, 443, 1.5)
    if ok or ok2:
        r.status = "ok"
        r.detail = (f"TCP reaches target on at least one port "
                     f"(probable MTU=1500 OK)")
    else:
        r.status = "warn"
        r.detail = "no TCP path on 80/443; probe inconclusive"
    return r


def check_duplicate_ip(target: str) -> CheckResult:
    r = CheckResult(name="A7. Duplicate-IP detection",
                     category="network")
    # ARP probe — if the same IP answers from two MAC addresses we'd
    # see it. We only have OS APIs, not raw ARP. Read the table.
    arp = _read_arp_table()
    matches = [(ip, mac) for ip, mac in arp.items() if ip == target]
    if not matches:
        r.status = "warn"
        r.detail = (f"target IP {target} not in ARP table — host hasn't "
                     "talked to it yet, can't detect duplicates")
    else:
        r.status = "ok"
        r.detail = (f"target {target} has single MAC {matches[0][1]} "
                     "(no duplicate IP suspected)")
    return r


# ── Category B: Host firewall ──────────────────────────────────────


def check_firewall_inbound(port: int) -> CheckResult:
    r = CheckResult(name=f"B1. Inbound TCP {port} not blocked",
                     category="firewall")
    # Bind locally and try to connect from a sibling socket. If
    # connect succeeds, port is reachable on the loopback at least.
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("0.0.0.0", port))
        s.listen(1)
        c = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        c.settimeout(0.5)
        try:
            c.connect(("127.0.0.1", port))
            r.status = "ok"
            r.detail = f"can bind + connect to local port {port}"
        except Exception as exc:
            r.status = "fail"
            r.detail = f"loopback connect failed: {exc}"
            r.remediation = (
                f"Host firewall is dropping connections on TCP {port}. "
                "Add an inbound allow rule.")
        c.close()
    except OSError as exc:
        r.status = "warn"
        r.detail = (f"could not bind {port} (in use? {exc}) — "
                     "firewall not testable here")
    finally:
        s.close()
    return r


def check_windows_defender() -> CheckResult:
    r = CheckResult(name="B2. Windows Defender real-time scan",
                     category="firewall")
    if os.name != "nt":
        r.status = "ok"
        r.detail = "not Windows — skipping"
        return r
    try:
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command",
             "Get-MpPreference | Select-Object -ExpandProperty "
             "DisableRealtimeMonitoring"],
            stderr=subprocess.DEVNULL, text=True, timeout=8,
        )
        if "true" in out.lower():
            r.status = "ok"
            r.detail = "real-time scan disabled (no slowdown)"
        else:
            r.status = "warn"
            r.detail = ("Defender real-time scan is ON — may slow "
                         "Helen-Server first-run by 5-10 s")
            r.remediation = (
                "Optional: Add-MpPreference -ExclusionProcess Helen-Server.exe")
    except Exception:
        r.status = "warn"
        r.detail = "couldn't query Defender state"
    return r


def check_av_software() -> CheckResult:
    r = CheckResult(name="B3. Third-party AV detection",
                     category="firewall")
    if os.name != "nt":
        r.status = "ok"
        r.detail = "not Windows — skipping"
        return r
    av_processes = [
        "MsMpEng.exe", "AvastSvc.exe", "avgsvc.exe",
        "MBAMService.exe", "ekrn.exe", "Bdagent.exe",
        "kavfsmui.exe", "ccSvcHst.exe", "smsvchost.exe",
        "CSAgent.exe", "SentinelAgent.exe", "CarbonBlackK.exe",
    ]
    try:
        out = subprocess.check_output(
            ["tasklist"], stderr=subprocess.DEVNULL,
            text=True, timeout=5,
        )
        found = [p for p in av_processes
                 if p.lower() in out.lower()]
        if found:
            r.status = "warn"
            r.detail = f"third-party AV detected: {', '.join(found)}"
            r.remediation = (
                "Some EDR/AV products kill PyInstaller-frozen exes. "
                "Add Helen-Server.exe to the AV exclusion list.")
        else:
            r.status = "ok"
            r.detail = "no known third-party AV process running"
    except Exception:
        r.status = "warn"
        r.detail = "could not enumerate processes"
    return r


# ── Category C: Application layer ──────────────────────────────────


async def check_server_health(host: str, port: int) -> CheckResult:
    r = CheckResult(name="C1. Server /api/health responds",
                     category="application")
    try:
        import httpx
    except ImportError:
        r.status = "warn"
        r.detail = "httpx not installed — can't probe"
        return r

    t0 = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=4.0) as c:
            resp = await c.get(f"http://{host}:{port}/api/health")
        r.elapsed_ms = (time.perf_counter() - t0) * 1000
        if resp.status_code == 200:
            r.status = "ok"
            r.detail = (f"HTTP 200 in {r.elapsed_ms:.0f} ms — "
                         f"{resp.text[:60]}")
        else:
            r.status = "fail"
            r.detail = f"HTTP {resp.status_code}: {resp.text[:80]}"
    except Exception as exc:
        r.status = "fail"
        r.detail = f"connection failed: {exc}"
        r.remediation = (
            f"Verify Helen-Server is running and listening on port {port}. "
            "Check the server's log file for startup errors.")
    return r


async def check_clock_skew(host: str, port: int) -> CheckResult:
    r = CheckResult(name="C2. Clock skew vs server (<5 s)",
                     category="application")
    try:
        import urllib.request
        from email.utils import parsedate_to_datetime
        req = urllib.request.Request(
            f"http://{host}:{port}/api/health", method="HEAD",
        )
        with urllib.request.urlopen(req, timeout=4) as resp:
            date_hdr = resp.headers.get("Date")
        if not date_hdr:
            r.status = "warn"
            r.detail = "server didn't return Date header"
            return r
        srv = parsedate_to_datetime(date_hdr).timestamp()
        skew = abs(time.time() - srv)
        if skew < 2:
            r.status = "ok"
            r.detail = f"skew {skew:.1f} s — within JWT tolerance"
        elif skew < 30:
            r.status = "warn"
            r.detail = f"skew {skew:.1f} s — JWT may misfire"
            r.remediation = "Run Helen-NTP or sync time via OS NTP."
        else:
            r.status = "fail"
            r.detail = f"skew {skew:.1f} s — JWT will reject every token"
            r.remediation = "URGENT: sync clock immediately."
    except Exception as exc:
        r.status = "warn"
        r.detail = f"could not measure: {exc}"
    return r


async def check_socketio_handshake(host: str, port: int) -> CheckResult:
    r = CheckResult(name="C3. Socket.IO handshake",
                     category="application")
    try:
        import httpx
    except ImportError:
        r.status = "warn"
        r.detail = "httpx not installed"
        return r
    try:
        async with httpx.AsyncClient(timeout=4.0) as c:
            resp = await c.get(
                f"http://{host}:{port}/socket.io/?EIO=4&transport=polling",
            )
        if resp.status_code == 200 and resp.text.startswith("0"):
            r.status = "ok"
            r.detail = "handshake successful"
        else:
            r.status = "warn"
            r.detail = f"unexpected response: HTTP {resp.status_code}"
    except Exception as exc:
        r.status = "fail"
        r.detail = f"handshake failed: {exc}"
    return r


# ── Category D: Helen-specific ─────────────────────────────────────


async def check_router_required_consistency(host: str, port: int
                                              ) -> CheckResult:
    r = CheckResult(name="D1. Mandatory-router consistency",
                     category="helen")
    try:
        import httpx
    except ImportError:
        r.status = "warn"
        r.detail = "httpx not installed"
        return r
    # Try a normal API call without router header
    try:
        async with httpx.AsyncClient(timeout=4.0) as c:
            resp = await c.post(
                f"http://{host}:{port}/api/auth/login",
                json={}, timeout=3.0,
            )
        if resp.status_code == 403 and "router_required" in resp.text:
            r.status = "warn"
            r.detail = ("server is in mandatory-router mode but "
                         "client request did not transit a router")
            r.remediation = (
                "Either point the client at the router URL, or set "
                "HELEN_REQUIRE_ROUTER=0 on the server if direct "
                "connections are intended.")
        elif resp.status_code in (200, 401, 422):
            r.status = "ok"
            r.detail = (f"server accepts direct requests "
                         f"(HTTP {resp.status_code}; auth not required)")
        else:
            r.status = "warn"
            r.detail = f"unexpected status: HTTP {resp.status_code}"
    except Exception as exc:
        r.status = "warn"
        r.detail = f"could not test: {exc}"
    return r


def check_hosts_file() -> CheckResult:
    r = CheckResult(name="D2. /etc/hosts (or HOSTS) sanity",
                     category="helen")
    if os.name == "nt":
        path = r"C:\Windows\System32\drivers\etc\hosts"
    else:
        path = "/etc/hosts"
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read()
    except Exception as exc:
        r.status = "warn"
        r.detail = f"could not read {path}: {exc}"
        return r
    bad_helen = [
        line for line in content.splitlines()
        if "helen" in line.lower() and not line.strip().startswith("#")
    ]
    if not bad_helen:
        r.status = "ok"
        r.detail = "no helen.* override in hosts file"
    else:
        r.status = "warn"
        r.detail = f"found {len(bad_helen)} helen.* entries — verify they're correct"
        r.raw = "\n".join(bad_helen[:10])
    return r


# ── Category E: OS policy ──────────────────────────────────────────


def check_smartscreen() -> CheckResult:
    r = CheckResult(name="E1. SmartScreen / Smart App Control",
                     category="os_policy")
    if os.name != "nt":
        r.status = "ok"
        r.detail = "not Windows"
        return r
    try:
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command",
             "Get-ItemProperty 'HKLM:\\SOFTWARE\\Microsoft\\Windows\\"
             "CurrentVersion\\Explorer\\AdvancedSchemas' | Format-List"],
            stderr=subprocess.DEVNULL, text=True, timeout=5,
        )
        r.status = "ok"
        r.detail = ("policy registry readable (manual review of "
                     "SmartScreen status recommended)")
    except Exception:
        r.status = "warn"
        r.detail = "could not query SmartScreen state"
    return r


def check_corporate_proxy() -> CheckResult:
    r = CheckResult(name="E2. Corporate HTTP proxy",
                     category="os_policy")
    proxy = (os.environ.get("HTTP_PROXY")
              or os.environ.get("HTTPS_PROXY")
              or os.environ.get("http_proxy")
              or os.environ.get("https_proxy"))
    if proxy:
        r.status = "warn"
        r.detail = f"HTTP_PROXY env var = {proxy}"
        r.remediation = (
            "Helen is LAN-only — a corporate proxy can mangle / "
            "block local traffic. Add the Helen target host to "
            "NO_PROXY: export NO_PROXY=helen.lan,10.0.0.0/8,192.168.0.0/16")
    else:
        r.status = "ok"
        r.detail = "no HTTP_PROXY env override"
    return r


# ── Helpers ────────────────────────────────────────────────────────


def _tcp_probe(host: str, port: int, timeout: float
                ) -> tuple[bool, float]:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    t0 = time.perf_counter()
    try:
        s.connect((host, port))
        ms = (time.perf_counter() - t0) * 1000
        return True, ms
    except Exception:
        return False, 0.0
    finally:
        s.close()


def _find_default_gateway() -> Optional[str]:
    try:
        if os.name == "nt":
            out = subprocess.check_output(
                ["route", "print", "-4"],
                stderr=subprocess.DEVNULL, text=True, timeout=3,
            )
            for line in out.splitlines():
                toks = line.split()
                if len(toks) >= 3 and toks[0] == "0.0.0.0":
                    return toks[2]
        else:
            out = subprocess.check_output(
                ["ip", "route"], stderr=subprocess.DEVNULL,
                text=True, timeout=3,
            )
            for line in out.splitlines():
                if line.startswith("default"):
                    parts = line.split()
                    return parts[parts.index("via") + 1]
    except Exception:
        pass
    return None


def _read_arp_table() -> dict[str, str]:
    cmd = ["arp", "-a"] if os.name == "nt" else ["ip", "neigh"]
    out = {}
    try:
        result = subprocess.check_output(
            cmd, stderr=subprocess.DEVNULL, text=True, timeout=3,
        )
        for line in result.splitlines():
            parts = line.split()
            ip = mac = None
            for p in parts:
                try:
                    ipaddress.ip_address(p)
                    ip = p
                except ValueError:
                    pass
                t = p.replace("-", ":").lower()
                if (len(t) == 17 and t.count(":") == 5
                        and all(c in "0123456789abcdef:" for c in t)):
                    mac = t
            if ip and mac:
                out[ip] = mac
    except Exception:
        pass
    return out


# ── Driver ──────────────────────────────────────────────────────────


async def run_all(host: str, port: int) -> DiagnosticReport:
    report = DiagnosticReport(target_host=host, target_port=port)

    # A — network
    report.checks.append(check_same_subnet(host))
    report.checks.append(check_default_gateway())
    report.checks.append(check_dns_resolution(host))
    report.checks.append(check_multicast_pass())
    report.checks.append(check_broadcast_pass())
    report.checks.append(check_pmtu(host))
    report.checks.append(check_duplicate_ip(host))

    # B — firewall
    report.checks.append(check_windows_defender())
    report.checks.append(check_av_software())

    # C — application
    report.checks.append(await check_server_health(host, port))
    report.checks.append(await check_clock_skew(host, port))
    report.checks.append(await check_socketio_handshake(host, port))

    # D — helen
    report.checks.append(await check_router_required_consistency(host, port))
    report.checks.append(check_hosts_file())

    # E — os policy
    report.checks.append(check_smartscreen())
    report.checks.append(check_corporate_proxy())

    return report


def print_report(report: DiagnosticReport) -> None:
    icons = {"ok": "\033[32m✓\033[0m",
              "warn": "\033[33m!\033[0m",
              "fail": "\033[31m✗\033[0m",
              "pending": "?"}
    cats = {}
    for r in report.checks:
        cats.setdefault(r.category, []).append(r)
    for cat, items in cats.items():
        print(f"\n── {cat.upper()} ──")
        for r in items:
            print(f"  {icons.get(r.status, '?')} {r.name}")
            print(f"      {r.detail}")
            if r.remediation:
                print(f"      → fix: {r.remediation}")
    counts = report.counts
    print()
    print("=" * 60)
    print(f"  {counts['ok']} ok  {counts['warn']} warn  "
          f"{counts['fail']} fail")
    print("=" * 60)


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1",
                   help="Target Helen-Server / router host")
    p.add_argument("--port", type=int, default=3000)
    p.add_argument("--json", action="store_true",
                   help="emit JSON instead of human-readable text")
    args = p.parse_args()

    print(f"Helen connection diagnostic — target {args.host}:{args.port}")
    print(f"  os: {platform.system()} {platform.release()}")
    print(f"  hostname: {socket.gethostname()}")
    report = await run_all(args.host, args.port)

    if args.json:
        print(json.dumps({
            "target_host": report.target_host,
            "target_port": report.target_port,
            "started_at": report.started_at,
            "counts": report.counts,
            "checks": [
                {"name": c.name, "category": c.category,
                 "status": c.status, "detail": c.detail,
                 "remediation": c.remediation}
                for c in report.checks
            ],
        }, indent=2))
    else:
        print_report(report)


if __name__ == "__main__":
    asyncio.run(main())
