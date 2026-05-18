"""
Windows Firewall auto-provisioning.

On startup, if we're running as administrator, ensure the firewall
has inbound rules for every port Helen uses. Non-admin processes
silently skip — the server still starts; the user just has to add
rules manually (or re-run as admin once).

Rules are keyed on a stable name ("Helen-Server-{kind}-{port}") so
re-running is idempotent and we don't pile up duplicate entries.

Why not use a proper MSI/NSIS post-install hook:
  * Operators restart the server from a fresh clone or a CI build
    without re-running the installer. This preserves the "it just
    works after upgrade" guarantee.
  * The rule-name prefix lets an ops person `netsh advfirewall
    firewall show rule name=Helen-*` to audit what we've added.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
from typing import Iterable

from app.core.logging import get_logger

logger = get_logger(__name__)


# (kind, port, protocol, description) — every inbound rule we want.
_RULES: tuple[tuple[str, int, str, str], ...] = (
    ("http", 3000, "TCP", "Helen REST/Socket.IO"),
    ("peer", 3001, "TCP", "Helen embedded peer server"),
    ("https", 3443, "TCP", "Helen HTTPS sidecar (phone pairing)"),
    ("discovery", 41234, "UDP", "Helen UDP broadcast discovery"),
    ("mdns", 5353, "UDP", "Helen mDNS advertisement"),
)


def _is_admin() -> bool:
    """Returns True iff the current process has Windows admin rights.
    Uses the IsUserAnAdmin shell API — available since Windows XP and
    returns 0/1 without throwing on non-Windows (the whole module is
    Windows-only anyway; the import guard below keeps Linux happy)."""
    if os.name != "nt":
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
    except Exception:
        return False


def _run_netsh(args: list[str]) -> tuple[int, str]:
    """Invoke netsh advfirewall firewall and return (rc, combined_output)."""
    try:
        proc = subprocess.run(
            ["netsh", "advfirewall", "firewall"] + args,
            capture_output=True, text=True, timeout=6.0, check=False,
        )
        return proc.returncode, (proc.stdout + proc.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)


def _rule_exists(name: str) -> bool:
    rc, out = _run_netsh(["show", "rule", f"name={name}"])
    return rc == 0 and "No rules match" not in out


def _add_rule(name: str, port: int, protocol: str, description: str) -> bool:
    """Add or replace a rule. `action=allow dir=in` + LAN-scoped so we
    don't punch holes for internet-origin traffic (remoteip=LocalSubnet
    matches the machine's direct-connected subnets)."""
    if _rule_exists(name):
        return True
    rc, out = _run_netsh([
        "add", "rule",
        f"name={name}",
        "dir=in",
        "action=allow",
        f"protocol={protocol}",
        f"localport={port}",
        "profile=private,domain",
        "remoteip=LocalSubnet",
        f"description={description}",
    ])
    if rc == 0:
        logger.info("firewall_rule_added", name=name, port=port, protocol=protocol)
        return True
    logger.warning("firewall_rule_add_failed", name=name, rc=rc, out=out[:200])
    return False


def ensure_firewall_rules() -> dict[str, object]:
    """Top-level entry. Returns a summary suitable for logging or
    surfacing in the admin diagnostic panel."""
    if os.name != "nt":
        return {"skipped": "not Windows"}
    if not _is_admin():
        logger.info("firewall_provision_skipped",
                    reason="not running as admin")
        return {"skipped": "not running as administrator",
                "hint": "restart Helen-Server as admin once, or add "
                        "rules manually: netsh advfirewall firewall add "
                        "rule name=Helen dir=in action=allow protocol=TCP "
                        "localport=3000,3001,3443"}
    added = 0
    existed = 0
    failed = 0
    for kind, port, protocol, desc in _RULES:
        name = f"Helen-Server-{kind}-{port}"
        if _rule_exists(name):
            existed += 1
            continue
        if _add_rule(name, port, protocol, desc):
            added += 1
        else:
            failed += 1
    return {
        "added": added,
        "already_present": existed,
        "failed": failed,
        "total_rules": len(_RULES),
    }
