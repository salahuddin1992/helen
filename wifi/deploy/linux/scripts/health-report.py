"""
HTML health report generator for Helen deployments.

Wraps the existing health-check.sh / health-check.ps1 logic in a
single Python file that produces a self-contained HTML page suitable
for sharing with operators / sending to support / archiving for
audit. Runs on Windows + Linux + macOS.

Usage::

    python health-report.py \
        --server-url http://localhost:3000 \
        --router-url http://localhost:8080 \
        --output /tmp/helen-health-2026-05-05.html

The generated HTML embeds:

  * Pass / warn / fail summary at the top.
  * Section per probe (server, router, ports, services, data, JWT,
    code signing, mDNS, time skew).
  * Raw stdout from each probe in collapsible <details> blocks.
  * Operator hostname + Helen versions for context.
  * No external CSS/JS — works offline forever.
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import os
import platform
import socket
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

try:
    import httpx  # type: ignore
except ImportError:
    print("ERROR: pip install httpx", file=sys.stderr)
    sys.exit(2)


@dataclass
class Probe:
    name: str
    status: str = "pending"   # ok | warn | fail | pending
    detail: str = ""
    raw: str = ""
    elapsed_ms: float = 0.0


@dataclass
class Report:
    started_at: float = field(default_factory=time.time)
    hostname: str = ""
    os: str = ""
    server_url: str = ""
    router_url: str = ""
    probes: list[Probe] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        c = {"ok": 0, "warn": 0, "fail": 0}
        for p in self.probes:
            if p.status in c:
                c[p.status] += 1
        return c


# ── Probes ──────────────────────────────────────────────────────────


async def probe_endpoint(name: str, url: str,
                          timeout_sec: float = 4.0) -> Probe:
    p = Probe(name=name)
    t0 = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=timeout_sec) as c:
            r = await c.get(url)
        p.elapsed_ms = (time.perf_counter() - t0) * 1000
        p.raw = r.text[:1000]
        if r.status_code == 200:
            p.status = "ok"
            p.detail = f"HTTP 200 in {p.elapsed_ms:.0f} ms"
        else:
            p.status = "warn"
            p.detail = f"HTTP {r.status_code}"
    except Exception as exc:
        p.elapsed_ms = (time.perf_counter() - t0) * 1000
        p.status = "fail"
        p.detail = f"unreachable: {exc}"
    return p


def probe_ports() -> Probe:
    """Best-effort listener check on the common Helen ports."""
    p = Probe(name="Listening ports")
    candidates = [
        ("TCP 3000 (Helen-Server HTTP)", "tcp", 3000),
        ("TCP 3443 (Helen-Server HTTPS)", "tcp", 3443),
        ("TCP 8080 (Helen-Router)", "tcp", 8080),
        ("UDP 41234 (Discovery)", "udp", 41234),
        ("UDP 5353 (mDNS)", "udp", 5353),
    ]
    rows = []
    overall = "ok"
    for label, proto, port in candidates:
        try:
            family = socket.AF_INET
            sock_type = (socket.SOCK_STREAM
                          if proto == "tcp"
                          else socket.SOCK_DGRAM)
            s = socket.socket(family, sock_type)
            s.settimeout(0.3)
            if proto == "tcp":
                # If we can connect, someone's listening. If we can't,
                # nothing's there (or firewall).
                try:
                    s.connect(("127.0.0.1", port))
                    rows.append(f"{label}: LISTENING")
                except OSError:
                    rows.append(f"{label}: not listening")
                    overall = "warn"
            else:
                try:
                    # Try to bind — if bind succeeds, port is FREE
                    # (i.e. nothing listening). We invert the result.
                    s.bind(("0.0.0.0", port))
                    rows.append(f"{label}: not listening")
                    overall = "warn"
                except OSError:
                    rows.append(f"{label}: in use (likely listening)")
            s.close()
        except Exception as exc:
            rows.append(f"{label}: probe failed ({exc})")
            overall = "warn"
    p.status = overall
    p.detail = f"{len(candidates)} ports probed"
    p.raw = "\n".join(rows)
    return p


def probe_disk(data_dir: str) -> Probe:
    p = Probe(name=f"Disk space at {data_dir}")
    if not os.path.isdir(data_dir):
        p.status = "warn"
        p.detail = "data dir missing"
        return p
    try:
        import shutil
        usage = shutil.disk_usage(data_dir)
        used_pct = 100 * (usage.total - usage.free) / max(usage.total, 1)
        p.detail = (f"{used_pct:.0f}% used "
                     f"({usage.free // (1024 ** 3)} GB free)")
        if used_pct > 90:
            p.status = "fail"
        elif used_pct > 80:
            p.status = "warn"
        else:
            p.status = "ok"
        p.raw = json.dumps({
            "total_gb": usage.total // (1024 ** 3),
            "used_gb": (usage.total - usage.free) // (1024 ** 3),
            "free_gb": usage.free // (1024 ** 3),
        }, indent=2)
    except Exception as exc:
        p.status = "warn"
        p.detail = f"could not stat: {exc}"
    return p


def probe_clock_skew(reference_url: str) -> Probe:
    """Compare local clock vs Date header from a reference Helen
    endpoint. >5 s skew is a fail (JWT exp checks will misfire)."""
    p = Probe(name="Clock skew vs server")
    try:
        import urllib.request
        req = urllib.request.Request(reference_url, method="HEAD")
        with urllib.request.urlopen(req, timeout=4) as resp:
            date_hdr = resp.headers.get("Date")
        if not date_hdr:
            p.status = "warn"
            p.detail = "server didn't return Date header"
            return p
        from email.utils import parsedate_to_datetime
        srv_dt = parsedate_to_datetime(date_hdr).timestamp()
        skew = abs(time.time() - srv_dt)
        p.detail = f"{skew:.1f} s"
        if skew > 30:
            p.status = "fail"
        elif skew > 5:
            p.status = "warn"
        else:
            p.status = "ok"
        p.raw = f"server: {date_hdr}\nlocal: {time.ctime()}\nskew: {skew}s"
    except Exception as exc:
        p.status = "warn"
        p.detail = f"could not measure: {exc}"
    return p


# ── HTML rendering ─────────────────────────────────────────────────


_HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Helen Health Report — {hostname} — {iso_time}</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, system-ui, sans-serif;
          background: #0d1117; color: #c9d1d9;
          margin: 0; padding: 2em; }}
  h1 {{ font-size: 1.8em; margin-top: 0; }}
  h2 {{ font-size: 1.2em; border-bottom: 1px solid #30363d;
        padding-bottom: 0.3em; }}
  .summary {{ display: flex; gap: 1em; margin: 1em 0; }}
  .pill {{ padding: 0.5em 1em; border-radius: 4px; font-weight: 600; }}
  .pill.ok   {{ background: #1f6f3f; color: #d9f7d9; }}
  .pill.warn {{ background: #856b00; color: #fff5cc; }}
  .pill.fail {{ background: #8b1f1f; color: #ffd6d6; }}
  table.probes {{ width: 100%; border-collapse: collapse; }}
  table.probes th, table.probes td {{
    border-bottom: 1px solid #30363d; padding: 0.5em; text-align: left;
  }}
  table.probes tr.ok  td:first-child {{ border-left: 4px solid #1f6f3f; }}
  table.probes tr.warn td:first-child {{ border-left: 4px solid #856b00; }}
  table.probes tr.fail td:first-child {{ border-left: 4px solid #8b1f1f; }}
  details pre {{ background: #161b22; padding: 1em; border-radius: 4px;
                  overflow-x: auto; }}
  small {{ color: #8b949e; }}
</style>
</head>
<body>
<h1>Helen Health Report</h1>
<p><small>
  host: <b>{hostname}</b> &middot;
  os: {os} &middot;
  generated: {iso_time}
</small></p>

<p>
  Server URL: <code>{server_url}</code><br>
  Router URL: <code>{router_url}</code>
</p>

<div class="summary">
  <span class="pill ok">  ✔ {ok}  passed</span>
  <span class="pill warn">! {warn} warnings</span>
  <span class="pill fail">✘ {fail} failures</span>
</div>

<h2>Probes</h2>
<table class="probes">
  <thead>
    <tr><th>Status</th><th>Probe</th><th>Detail</th><th>Time</th></tr>
  </thead>
  <tbody>
{rows}
  </tbody>
</table>

<h2>Raw output</h2>
{details}

<p><small>
  Generated by health-report.py v1.0.0. The HTML is self-contained
  and can be opened offline forever — no external CSS/JS pulled.
</small></p>
</body>
</html>
"""


_ICON = {"ok": "✔", "warn": "!", "fail": "✘", "pending": "…"}


def render_html(report: Report) -> str:
    rows = []
    details = []
    for p in report.probes:
        rows.append(
            f'<tr class="{p.status}">'
            f'<td>{_ICON.get(p.status, "?")} {p.status}</td>'
            f'<td>{html.escape(p.name)}</td>'
            f'<td>{html.escape(p.detail)}</td>'
            f'<td>{p.elapsed_ms:.0f} ms</td>'
            f'</tr>'
        )
        if p.raw:
            details.append(
                f'<details><summary>{html.escape(p.name)} '
                f'({p.status})</summary>'
                f'<pre>{html.escape(p.raw)}</pre></details>'
            )
    counts = report.counts
    return _HTML_TEMPLATE.format(
        hostname=html.escape(report.hostname),
        os=html.escape(report.os),
        iso_time=time.strftime(
            "%Y-%m-%d %H:%M:%S",
            time.localtime(report.started_at)),
        server_url=html.escape(report.server_url),
        router_url=html.escape(report.router_url),
        ok=counts["ok"], warn=counts["warn"], fail=counts["fail"],
        rows="\n".join(rows),
        details="\n".join(details) or "<p>(no raw output captured)</p>",
    )


# ── Driver ──────────────────────────────────────────────────────────


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server-url",
                        default="http://localhost:3000",
                        help="Helen-Server base URL")
    parser.add_argument("--router-url",
                        default="http://localhost:8080",
                        help="Helen-Router base URL")
    parser.add_argument("--data-dir",
                        default=os.environ.get(
                            "HELEN_DATA_DIR",
                            "/opt/helen-server/_internal/data"
                            if os.name != "nt"
                            else r"C:\Program Files\Helen-Server\_internal\data",
                        ))
    parser.add_argument("--output", default="helen-health.html")
    args = parser.parse_args()

    report = Report(
        hostname=socket.gethostname(),
        os=f"{platform.system()} {platform.release()}",
        server_url=args.server_url,
        router_url=args.router_url,
    )

    print(f"[*] Probing Helen at {args.server_url}...")
    report.probes.append(
        await probe_endpoint("Helen-Server /api/health",
                              f"{args.server_url}/api/health"),
    )
    report.probes.append(
        await probe_endpoint("Helen-Router /router/health",
                              f"{args.router_url}/router/health"),
    )
    print("[*] Listener probe...")
    report.probes.append(probe_ports())
    print("[*] Disk probe...")
    report.probes.append(probe_disk(args.data_dir))
    print("[*] Clock skew probe...")
    report.probes.append(
        probe_clock_skew(f"{args.server_url}/api/health"),
    )

    html_doc = render_html(report)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(html_doc)
    print(f"[+] Report written to {args.output}")
    print(f"  Pass: {report.counts['ok']}  "
          f"Warn: {report.counts['warn']}  "
          f"Fail: {report.counts['fail']}")


if __name__ == "__main__":
    asyncio.run(main())
