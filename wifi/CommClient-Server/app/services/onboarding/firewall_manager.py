"""
FirewallManager — cross-OS firewall rule reader/writer.

Detects the running OS firewall and exposes a single rule schema:

    {
        "direction": "in" | "out",
        "action":    "allow" | "deny",
        "protocol":  "tcp" | "udp" | "any",
        "port_range": "PORT" | "PORT-PORT" | "any",
        "source_cidr": optional CIDR,
        "description": optional str,
    }

Backends
--------
Windows:  ``netsh advfirewall`` (parsing + apply)
Linux:    firewalld → nftables → iptables (probed in that order)
macOS:    ``pfctl`` reads, anchors-based writes
"""
from __future__ import annotations

import asyncio
import platform
import shutil
import subprocess
from dataclasses import dataclass, asdict
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass
class FirewallRule:
    direction: str            # in | out
    action: str               # allow | deny
    protocol: str             # tcp | udp | any
    port_range: str           # "443" | "1000-2000" | "any"
    source_cidr: str | None = None
    description: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "FirewallRule":
        direction = (d.get("direction") or "in").lower()
        if direction not in {"in", "out"}:
            direction = "in"
        action = (d.get("action") or "allow").lower()
        if action not in {"allow", "deny"}:
            action = "allow"
        protocol = (d.get("protocol") or "tcp").lower()
        if protocol not in {"tcp", "udp", "any"}:
            protocol = "tcp"
        return cls(
            direction=direction,
            action=action,
            protocol=protocol,
            port_range=str(d.get("port_range") or "any"),
            source_cidr=d.get("source_cidr"),
            description=str(d.get("description") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class FirewallManager:
    """Detect the active OS firewall and apply rules through it."""

    def __init__(self) -> None:
        self.os_name = platform.system().lower()
        self.backend = self._detect_backend()

    # ── detection ────────────────────────────────────────
    def _detect_backend(self) -> str:
        if self.os_name == "windows":
            return "netsh"
        if self.os_name == "darwin":
            return "pfctl" if shutil.which("pfctl") else "none"
        # Linux probe order
        if shutil.which("firewall-cmd"):
            return "firewalld"
        if shutil.which("nft"):
            return "nftables"
        if shutil.which("iptables"):
            return "iptables"
        return "none"

    def info(self) -> dict[str, Any]:
        return {
            "os": self.os_name,
            "backend": self.backend,
            "supported": self.backend != "none",
        }

    # ── public API ───────────────────────────────────────
    async def get_rules(self) -> list[dict[str, Any]]:
        return await asyncio.get_event_loop().run_in_executor(None, self._get_rules_sync)

    async def apply_rules(self, rules: list[Any]) -> dict[str, Any]:
        normalized: list[FirewallRule] = []
        for r in rules:
            if isinstance(r, FirewallRule):
                normalized.append(r)
            elif isinstance(r, dict):
                normalized.append(FirewallRule.from_dict(r))
        return await asyncio.get_event_loop().run_in_executor(
            None, self._apply_rules_sync, normalized,
        )

    # ── sync workers ─────────────────────────────────────
    def _get_rules_sync(self) -> list[dict[str, Any]]:
        try:
            if self.backend == "netsh":
                return self._netsh_list()
            if self.backend == "firewalld":
                return self._firewalld_list()
            if self.backend == "nftables":
                return self._nft_list()
            if self.backend == "iptables":
                return self._iptables_list()
            if self.backend == "pfctl":
                return self._pfctl_list()
        except Exception as e:
            logger.warning("firewall_list_failed",
                           backend=self.backend, error=str(e))
        return []

    def _apply_rules_sync(self, rules: list[FirewallRule]) -> dict[str, Any]:
        applied: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        for r in rules:
            try:
                self._apply_one(r)
                applied.append(r.to_dict())
            except Exception as e:
                logger.warning("firewall_apply_failed",
                               rule=r.to_dict(), error=str(e))
                failed.append({**r.to_dict(), "error": str(e)})
        return {"backend": self.backend, "applied": applied, "failed": failed}

    def _apply_one(self, rule: FirewallRule) -> None:
        if self.backend == "netsh":
            return self._netsh_add(rule)
        if self.backend == "firewalld":
            return self._firewalld_add(rule)
        if self.backend == "nftables":
            return self._nft_add(rule)
        if self.backend == "iptables":
            return self._iptables_add(rule)
        if self.backend == "pfctl":
            return self._pfctl_add(rule)
        raise RuntimeError(f"no firewall backend available ({self.os_name})")

    # ── Windows netsh ────────────────────────────────────
    def _netsh_list(self) -> list[dict[str, Any]]:
        cp = subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule", "name=all"],
            capture_output=True, text=True, timeout=15, check=False,
        )
        rules: list[dict[str, Any]] = []
        current: dict[str, Any] = {}
        for line in cp.stdout.splitlines():
            line = line.strip()
            if line.startswith("Rule Name:"):
                if current:
                    rules.append(current)
                current = {"description": line.split(":", 1)[1].strip()}
            elif line.startswith("Direction:"):
                v = line.split(":", 1)[1].strip().lower()
                current["direction"] = "in" if v.startswith("in") else "out"
            elif line.startswith("Action:"):
                current["action"] = line.split(":", 1)[1].strip().lower()
            elif line.startswith("Protocol:"):
                current["protocol"] = line.split(":", 1)[1].strip().lower()
            elif line.startswith("LocalPort:"):
                current["port_range"] = line.split(":", 1)[1].strip()
        if current:
            rules.append(current)
        return rules

    def _netsh_add(self, r: FirewallRule) -> None:
        name = f"Helen-{r.protocol}-{r.port_range}-{r.direction}"
        cmd = [
            "netsh", "advfirewall", "firewall", "add", "rule",
            f"name={name}",
            f"dir={r.direction}",
            f"action={r.action}",
            f"protocol={r.protocol if r.protocol != 'any' else 'any'}",
        ]
        if r.protocol != "any" and r.port_range and r.port_range != "any":
            cmd.append(f"localport={r.port_range}")
        if r.source_cidr:
            cmd.append(f"remoteip={r.source_cidr}")
        cp = subprocess.run(cmd, capture_output=True, text=True, timeout=15, check=False)
        if cp.returncode != 0:
            raise RuntimeError(cp.stderr.strip() or "netsh failed")

    # ── firewalld ────────────────────────────────────────
    def _firewalld_list(self) -> list[dict[str, Any]]:
        cp = subprocess.run(
            ["firewall-cmd", "--list-all"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        return [{"raw": cp.stdout}]

    def _firewalld_add(self, r: FirewallRule) -> None:
        if r.port_range == "any":
            raise RuntimeError("firewalld requires explicit port")
        port_spec = f"{r.port_range}/{r.protocol if r.protocol != 'any' else 'tcp'}"
        action = "--add-port" if r.action == "allow" else "--remove-port"
        cp = subprocess.run(
            ["firewall-cmd", action + "=" + port_spec, "--permanent"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if cp.returncode != 0:
            raise RuntimeError(cp.stderr.strip() or "firewall-cmd failed")
        subprocess.run(["firewall-cmd", "--reload"],
                       capture_output=True, text=True, timeout=10, check=False)

    # ── nftables ─────────────────────────────────────────
    def _nft_list(self) -> list[dict[str, Any]]:
        cp = subprocess.run(["nft", "list", "ruleset"],
                            capture_output=True, text=True, timeout=10, check=False)
        return [{"raw": cp.stdout}]

    def _nft_add(self, r: FirewallRule) -> None:
        proto = r.protocol if r.protocol != "any" else "tcp"
        verdict = "accept" if r.action == "allow" else "drop"
        port = r.port_range if r.port_range != "any" else "0-65535"
        chain = "input" if r.direction == "in" else "output"
        rule_text = f"add rule inet filter {chain} {proto} dport {{ {port} }} {verdict}"
        cp = subprocess.run(["nft", rule_text],
                            capture_output=True, text=True, timeout=10, check=False)
        if cp.returncode != 0:
            raise RuntimeError(cp.stderr.strip() or "nft failed")

    # ── iptables ─────────────────────────────────────────
    def _iptables_list(self) -> list[dict[str, Any]]:
        cp = subprocess.run(["iptables", "-S"],
                            capture_output=True, text=True, timeout=10, check=False)
        return [{"raw": cp.stdout}]

    def _iptables_add(self, r: FirewallRule) -> None:
        chain = "INPUT" if r.direction == "in" else "OUTPUT"
        proto = r.protocol if r.protocol != "any" else "tcp"
        target = "ACCEPT" if r.action == "allow" else "DROP"
        cmd = ["iptables", "-A", chain, "-p", proto]
        if r.port_range and r.port_range != "any":
            cmd += ["--dport", r.port_range.replace("-", ":")]
        if r.source_cidr:
            cmd += ["-s", r.source_cidr]
        cmd += ["-j", target]
        cp = subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=False)
        if cp.returncode != 0:
            raise RuntimeError(cp.stderr.strip() or "iptables failed")

    # ── macOS pf ─────────────────────────────────────────
    def _pfctl_list(self) -> list[dict[str, Any]]:
        cp = subprocess.run(["pfctl", "-sr"],
                            capture_output=True, text=True, timeout=10, check=False)
        return [{"raw": cp.stdout}]

    def _pfctl_add(self, r: FirewallRule) -> None:
        # pfctl rules normally come from /etc/pf.conf; appending live rules
        # requires anchors which need root + filesystem access. We accept
        # the rule by writing it to a tmp anchor file.
        verb = "pass" if r.action == "allow" else "block"
        proto = r.protocol if r.protocol != "any" else "tcp"
        port = "" if r.port_range == "any" else f"port {r.port_range.replace('-', ':')}"
        src = f"from {r.source_cidr}" if r.source_cidr else "from any"
        rule_text = f"{verb} {('in' if r.direction == 'in' else 'out')} proto {proto} {src} to any {port}\n"
        try:
            cp = subprocess.run(
                ["pfctl", "-a", "helen/onboarding", "-f", "-"],
                input=rule_text, capture_output=True, text=True,
                timeout=10, check=False,
            )
            if cp.returncode != 0:
                raise RuntimeError(cp.stderr.strip() or "pfctl failed")
        except FileNotFoundError:
            raise RuntimeError("pfctl binary not found")
