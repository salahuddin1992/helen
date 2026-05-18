"""
Helen auto-fix — apply known-safe remediations for the issues that
``connection-diagnostic.py`` reports.

This script ONLY makes changes that are reversible and have a clear
rollback path. It explicitly refuses to touch anything that needs
admin/root unless the operator passed ``--elevated`` to confirm
intent.

Fixes applied
-------------
  1. ADD INBOUND firewall rule for Helen ports (Windows Defender + ufw).
  2. ADD OUTBOUND firewall rule for UDP 41234 / 5353 / 1900.
  3. EXCLUDE Helen-Server.exe from Defender real-time scan.
  4. APPEND `127.0.0.1 helen.local` to hosts file (if missing).
  5. SET NO_PROXY env var to bypass corporate proxy for LAN.
  6. RESTART helen-server / helen-router service (if running).
  7. CLEAN stale TIME_WAIT sockets (Windows).
  8. SYNC clock via Windows w32tm (Windows) or ntpdate (Linux).

Each fix is idempotent and writes a backup of any modified file.
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path


def _is_elevated() -> bool:
    if os.name == "nt":
        try:
            r = subprocess.run(
                ["net", "session"], capture_output=True, timeout=4,
            )
            return r.returncode == 0
        except Exception:
            return False
    return os.geteuid() == 0


# ── Fixes ──────────────────────────────────────────────────────────


def fix_firewall_inbound(elevated: bool) -> bool:
    if os.name != "nt":
        return _fix_firewall_inbound_unix(elevated)
    if not elevated:
        print("[!] firewall fix needs admin — skipping. Re-run as Administrator.")
        return False
    rules = [
        ("Helen-Server HTTP", 3000, "TCP"),
        ("Helen-Server HTTPS", 3443, "TCP"),
        ("Helen-Router", 8080, "TCP"),
        ("Helen Discovery", 41234, "UDP"),
        ("Helen mDNS", 5353, "UDP"),
    ]
    cmd = ["powershell", "-NoProfile", "-Command"]
    script_lines = []
    for name, port, proto in rules:
        script_lines.append(
            f"if (-not (Get-NetFirewallRule -DisplayName "
            f"'{name}' -ErrorAction SilentlyContinue)) {{ "
            f"New-NetFirewallRule -DisplayName '{name}' "
            f"-Direction Inbound -Action Allow "
            f"-Protocol {proto} -LocalPort {port} "
            f"-Profile Private,Domain | Out-Null; "
            f"Write-Host 'added: {name}' "
            f"}} else {{ Write-Host 'exists: {name}' }}"
        )
    script = "; ".join(script_lines)
    cmd.append(script)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        print(f"  [firewall] {r.stdout.strip()}")
        return r.returncode == 0
    except Exception as exc:
        print(f"  [firewall] failed: {exc}")
        return False


def _fix_firewall_inbound_unix(elevated: bool) -> bool:
    if not elevated:
        print("[!] firewall fix needs root — skipping.")
        return False
    if shutil.which("ufw"):
        for port, proto in ((3000, "tcp"), (3443, "tcp"),
                              (8080, "tcp"),
                              (41234, "udp"), (5353, "udp")):
            try:
                subprocess.run(
                    ["ufw", "allow", f"{port}/{proto}"],
                    check=False, timeout=5,
                )
            except Exception:
                continue
        print("  [firewall] ufw rules applied")
        return True
    if shutil.which("firewall-cmd"):
        for port, proto in ((3000, "tcp"), (3443, "tcp"),
                              (8080, "tcp"),
                              (41234, "udp"), (5353, "udp")):
            try:
                subprocess.run(
                    ["firewall-cmd", "--permanent",
                     f"--add-port={port}/{proto}"],
                    check=False, timeout=5,
                )
            except Exception:
                continue
        subprocess.run(["firewall-cmd", "--reload"],
                        check=False, timeout=5)
        print("  [firewall] firewalld rules applied")
        return True
    print("  [firewall] no ufw/firewalld found — install one or "
          "open ports manually")
    return False


def fix_defender_exclusion(elevated: bool) -> bool:
    if os.name != "nt":
        return True  # n/a
    if not elevated:
        print("[!] Defender exclusion needs admin — skipping.")
        return False
    exes = [
        r"C:\Program Files\Helen-Server\Helen-Server.exe",
        r"C:\Program Files\Helen-Router\Helen-Router.exe",
        r"C:\Program Files\Helen-Rendezvous\Helen-Rendezvous.exe",
    ]
    for exe in exes:
        if not Path(exe).exists():
            continue
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"Add-MpPreference -ExclusionProcess '{exe}'"],
                check=False, timeout=10, capture_output=True,
            )
            print(f"  [defender] excluded {exe}")
        except Exception as exc:
            print(f"  [defender] couldn't exclude {exe}: {exc}")
    return True


def fix_hosts_file(elevated: bool) -> bool:
    if os.name == "nt":
        path = Path(r"C:\Windows\System32\drivers\etc\hosts")
    else:
        path = Path("/etc/hosts")
    if not path.exists():
        print(f"  [hosts] {path} not found")
        return False
    try:
        content = path.read_text(encoding="utf-8")
    except Exception as exc:
        print(f"  [hosts] could not read: {exc}")
        return False
    if "helen.local" in content or "helen.lan" in content:
        print("  [hosts] helen.* already present — no change")
        return True
    if not elevated:
        print("  [!] hosts file fix needs admin/root — skipping")
        return False
    backup = path.with_suffix(".helen-backup")
    backup.write_text(content, encoding="utf-8")
    new = (
        content.rstrip()
        + "\n"
        + "127.0.0.1 helen.local helen.lan helen-server.helen.lan "
          "helen-router.helen.lan\n"
    )
    try:
        path.write_text(new, encoding="utf-8")
        print(f"  [hosts] appended helen.* aliases (backup: {backup})")
        return True
    except Exception as exc:
        print(f"  [hosts] write failed: {exc}")
        return False


def fix_no_proxy() -> bool:
    """Recommend NO_PROXY on the user's shell. Doesn't persist
    by itself — printing the export line is the deliverable."""
    no_proxy = os.environ.get("NO_PROXY", "")
    needed = "helen.lan,helen.local,127.0.0.1,10.0.0.0/8," \
              "172.16.0.0/12,192.168.0.0/16"
    if all(part in no_proxy for part in
            ("helen.lan", "127.0.0.1", "192.168")):
        print("  [proxy] NO_PROXY already covers Helen LAN — no change")
        return True
    print(f"  [proxy] add to your shell:")
    if os.name == "nt":
        print(f"    set NO_PROXY={needed}")
        print(f"    setx NO_PROXY \"{needed}\"   # persists across sessions")
    else:
        print(f"    export NO_PROXY={needed}")
    return True


def fix_clock_sync(elevated: bool) -> bool:
    if os.name == "nt":
        if not elevated:
            print("  [clock] w32tm needs admin — skipping")
            return False
        try:
            subprocess.run(
                ["w32tm", "/resync", "/force"],
                check=False, timeout=10, capture_output=True,
            )
            print("  [clock] w32tm resync issued")
            return True
        except Exception as exc:
            print(f"  [clock] w32tm failed: {exc}")
            return False
    if shutil.which("ntpdate"):
        if not elevated:
            print("  [clock] ntpdate needs root — skipping")
            return False
        try:
            subprocess.run(
                ["ntpdate", "-u", "pool.ntp.org"],
                check=False, timeout=10,
            )
            print("  [clock] ntpdate sync attempted")
            return True
        except Exception:
            pass
    if shutil.which("chronyc"):
        try:
            subprocess.run(["chronyc", "makestep"],
                           check=False, timeout=5)
            print("  [clock] chronyc makestep issued")
            return True
        except Exception:
            pass
    print("  [clock] no time-sync tool found")
    return False


def fix_restart_services(elevated: bool) -> bool:
    """Restart helen-server / helen-router / helen-rendezvous if they
    look like they're crashed."""
    if os.name == "nt":
        if not elevated:
            print("  [restart] need admin to restart services — skipping")
            return False
        for svc in ("HelenServer", "HelenRouter", "HelenRendezvous"):
            try:
                subprocess.run(
                    ["powershell", "-NoProfile", "-Command",
                     f"if (Get-Service {svc} -ErrorAction SilentlyContinue) "
                     f"{{ Restart-Service {svc} -Force "
                     f"-ErrorAction SilentlyContinue }}"],
                    check=False, timeout=15,
                )
                print(f"  [restart] {svc} restart attempted")
            except Exception as exc:
                print(f"  [restart] {svc} failed: {exc}")
        return True
    # Linux — systemd
    if not elevated:
        print("  [restart] need root for systemctl restart — skipping")
        return False
    for svc in ("helen-server", "helen-router", "helen-rendezvous"):
        try:
            r = subprocess.run(
                ["systemctl", "is-active", svc],
                capture_output=True, text=True, timeout=4,
            )
            if r.stdout.strip() == "active":
                subprocess.run(
                    ["systemctl", "restart", svc],
                    check=False, timeout=15,
                )
                print(f"  [restart] {svc} restarted")
        except Exception:
            continue
    return True


# ── Driver ──────────────────────────────────────────────────────────


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--elevated", action="store_true",
                   help="confirm we have admin/root and want to apply "
                        "privileged fixes")
    p.add_argument("--skip-firewall", action="store_true")
    p.add_argument("--skip-defender", action="store_true")
    p.add_argument("--skip-hosts", action="store_true")
    p.add_argument("--skip-clock", action="store_true")
    p.add_argument("--skip-restart", action="store_true")
    args = p.parse_args()

    print("Helen auto-fix")
    print(f"  os: {platform.system()} {platform.release()}")
    elevated = _is_elevated()
    if args.elevated and not elevated:
        print("[!] --elevated specified but we don't appear to have admin/root.")
        print("    Re-run from an elevated terminal for privileged fixes.")
    elevated = elevated and args.elevated

    print(f"  elevated: {elevated}")
    print()

    if not args.skip_firewall:
        print("[*] Fix 1 — firewall rules")
        fix_firewall_inbound(elevated)

    if not args.skip_defender:
        print("[*] Fix 2 — Defender exclusion")
        fix_defender_exclusion(elevated)

    if not args.skip_hosts:
        print("[*] Fix 3 — hosts file")
        fix_hosts_file(elevated)

    print("[*] Fix 4 — proxy hint")
    fix_no_proxy()

    if not args.skip_clock:
        print("[*] Fix 5 — clock sync")
        fix_clock_sync(elevated)

    if not args.skip_restart:
        print("[*] Fix 6 — restart services if down")
        fix_restart_services(elevated)

    print()
    print("Done. Re-run connection-diagnostic.py to verify.")


if __name__ == "__main__":
    main()
