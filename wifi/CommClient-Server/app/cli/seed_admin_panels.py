"""
Seed data utility for the 11 new admin panels.

Creates demo tenants, workspaces, users, plans, licenses, DR destinations,
backups, federation peers, holds, DSARs, RTBFs, classification rules, plugins,
retention policies, and onboarding state.

Idempotent: re-running upserts by deterministic slug/key.

Usage:
    python -m app.cli.seed_admin_panels --update           # upsert
    python -m app.cli.seed_admin_panels --reset            # wipe + recreate
    python -m app.cli.seed_admin_panels --scope billing    # one scope only
    python -m app.cli.seed_admin_panels --dry-run          # show plan, do not write

Scopes: tenancy, billing, dr, federation, compliance, plugins, audit, onboarding, all
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import secrets
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence

# ---------------------------------------------------------------------------
# Bootstrap & logging
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

logging.basicConfig(
    level=os.getenv("HELEN_SEED_LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)-7s | seed | %(message)s",
)
log = logging.getLogger("helen.seed")


SCOPES = (
    "tenancy",
    "billing",
    "dr",
    "federation",
    "compliance",
    "plugins",
    "audit",
    "onboarding",
)

UTC = timezone.utc


def now() -> datetime:
    return datetime.now(UTC)


def deterministic_id(prefix: str, *parts: Any) -> str:
    """Stable id derived from (prefix, *parts). Used for idempotency."""
    raw = "|".join([prefix, *map(str, parts)])
    h = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{h}"


# ---------------------------------------------------------------------------
# Seed plan
# ---------------------------------------------------------------------------


@dataclass
class SeedResult:
    scope: str
    created: int = 0
    updated: int = 0
    skipped: int = 0
    errors: List[str] = field(default_factory=list)

    def add_created(self) -> None:
        self.created += 1

    def add_updated(self) -> None:
        self.updated += 1

    def add_skipped(self) -> None:
        self.skipped += 1

    def add_error(self, msg: str) -> None:
        self.errors.append(msg)

    def merge(self, other: "SeedResult") -> None:
        self.created += other.created
        self.updated += other.updated
        self.skipped += other.skipped
        self.errors.extend(other.errors)


@dataclass
class SeedContext:
    dry_run: bool = False
    reset: bool = False
    update: bool = True
    scopes: Sequence[str] = field(default_factory=lambda: SCOPES)


# ---------------------------------------------------------------------------
# Demo data fixtures
# ---------------------------------------------------------------------------

DEMO_TENANTS = [
    {
        "id": deterministic_id("tenant", "acme"),
        "name": "Acme Corp",
        "slug": "acme",
        "plan": "enterprise",
        "status": "active",
        "region": "eu-on-prem-1",
        "admin_email": "admin@acme.local",
    },
    {
        "id": deterministic_id("tenant", "umbrella"),
        "name": "Umbrella Industries",
        "slug": "umbrella",
        "plan": "pro",
        "status": "active",
        "region": "me-on-prem-1",
        "admin_email": "ops@umbrella.local",
    },
    {
        "id": deterministic_id("tenant", "wayne"),
        "name": "Wayne Enterprises",
        "slug": "wayne",
        "plan": "free",
        "status": "active",
        "region": "us-on-prem-1",
        "admin_email": "bruce@wayne.local",
    },
]

DEMO_WORKSPACES = [
    {"tenant_slug": "acme", "name": "Engineering", "slug": "acme-eng"},
    {"tenant_slug": "acme", "name": "Operations", "slug": "acme-ops"},
    {"tenant_slug": "acme", "name": "Executive", "slug": "acme-exec"},
    {"tenant_slug": "umbrella", "name": "Research", "slug": "umbrella-r"},
    {"tenant_slug": "umbrella", "name": "Security", "slug": "umbrella-sec"},
    {"tenant_slug": "wayne", "name": "Default", "slug": "wayne-default"},
]

DEMO_USERS = [
    {"username": "ali.admin", "email": "ali@acme.local", "tenant": "acme", "role": "admin"},
    {"username": "sara.ops", "email": "sara@acme.local", "tenant": "acme", "role": "operator"},
    {"username": "youssef.dev", "email": "youssef@acme.local", "tenant": "acme", "role": "user"},
    {"username": "mona.audit", "email": "mona@acme.local", "tenant": "acme", "role": "auditor"},
    {"username": "khaled.sec", "email": "khaled@umbrella.local", "tenant": "umbrella", "role": "admin"},
    {"username": "noor.research", "email": "noor@umbrella.local", "tenant": "umbrella", "role": "user"},
    {"username": "fatma.ops", "email": "fatma@umbrella.local", "tenant": "umbrella", "role": "operator"},
    {"username": "bruce.wayne", "email": "bruce@wayne.local", "tenant": "wayne", "role": "admin"},
    {"username": "lucius.fox", "email": "lucius@wayne.local", "tenant": "wayne", "role": "operator"},
    {"username": "alfred.p", "email": "alfred@wayne.local", "tenant": "wayne", "role": "user"},
]

DEMO_PLANS = [
    {
        "code": "free",
        "name": "Free",
        "max_users": 10,
        "max_storage_gb": 5,
        "max_calls_per_day": 50,
        "features": ["calls", "messages"],
        "price_cents": 0,
    },
    {
        "code": "pro",
        "name": "Pro",
        "max_users": 100,
        "max_storage_gb": 100,
        "max_calls_per_day": 1000,
        "features": ["calls", "messages", "e2ee", "webhooks", "plugins"],
        "price_cents": 0,  # LAN-only, informational
    },
    {
        "code": "enterprise",
        "name": "Enterprise",
        "max_users": 10000,
        "max_storage_gb": 10000,
        "max_calls_per_day": 100000,
        "features": [
            "calls",
            "messages",
            "e2ee",
            "webhooks",
            "plugins",
            "federation",
            "bridges",
            "dr",
            "ai",
            "compliance",
            "zero_trust",
        ],
        "price_cents": 0,
    },
]

DEMO_LICENSES = [
    {
        "tenant_slug": "acme",
        "plan": "enterprise",
        "seats": 500,
        "duration_days": 365,
        "features": ["all"],
    },
    {
        "tenant_slug": "umbrella",
        "plan": "pro",
        "seats": 50,
        "duration_days": 90,
        "features": ["calls", "e2ee", "plugins"],
    },
]

DEMO_DR_DESTINATIONS = [
    {
        "name": "primary-nas",
        "type": "nfs",
        "config": {"host": "nas1.lan", "path": "/exports/helen-backups"},
        "capacity_gb": 4000,
    },
    {
        "name": "secondary-smb",
        "type": "smb",
        "config": {"share": "\\\\backupsrv\\helen", "username": "helen_bkp"},
        "capacity_gb": 2000,
    },
    {
        "name": "tape-vault",
        "type": "tape-lto",
        "config": {"library": "LTO9-1", "rotation": "weekly"},
        "capacity_gb": 18000,
    },
]

DEMO_BACKUP_POLICIES = [
    {
        "name": "nightly-full",
        "cron": "0 2 * * *",
        "scope": ["databases", "audit-chain", "configs"],
        "cadence": "full",
        "retention_days": 30,
        "encryption": "AES-256-GCM",
        "enabled": True,
    },
    {
        "name": "hourly-incremental",
        "cron": "15 * * * *",
        "scope": ["databases"],
        "cadence": "incremental",
        "retention_days": 7,
        "encryption": "AES-256-GCM",
        "enabled": True,
    },
    {
        "name": "weekly-full-archive",
        "cron": "0 3 * * 0",
        "scope": ["all"],
        "cadence": "full",
        "retention_days": 365,
        "encryption": "ChaCha20-Poly1305",
        "enabled": True,
    },
]

DEMO_FEDERATION_PEERS = [
    {
        "hostname": "helen-eu-1.lan",
        "ip": "10.20.30.10",
        "region": "eu-on-prem-1",
        "role": "master",
        "version": "1.0.0",
    },
    {
        "hostname": "helen-eu-2.lan",
        "ip": "10.20.30.11",
        "region": "eu-on-prem-1",
        "role": "follower",
        "version": "1.0.0",
    },
    {
        "hostname": "helen-me-1.lan",
        "ip": "10.30.40.10",
        "region": "me-on-prem-1",
        "role": "follower",
        "version": "1.0.0",
    },
    {
        "hostname": "helen-us-1.lan",
        "ip": "10.40.50.10",
        "region": "us-on-prem-1",
        "role": "observer",
        "version": "1.0.0",
    },
]

DEMO_LEGAL_HOLDS = [
    {
        "name": "Q1-2026-investigation",
        "case_ref": "INV-2026-001",
        "scope": {"custodians": ["ali.admin", "sara.ops"], "date_range": ["2026-01-01", "2026-04-01"]},
        "expires_at": (now() + timedelta(days=365)).isoformat(),
    },
    {
        "name": "compliance-review-2026",
        "case_ref": "LEG-2026-014",
        "scope": {"channels": ["acme-eng"], "keywords": ["GDPR", "PII"]},
        "expires_at": (now() + timedelta(days=180)).isoformat(),
    },
]

DEMO_DSAR = [
    {
        "subject": "user@external.local",
        "type": "access",
        "identity_verified": True,
        "deadline_days": 30,
    },
    {
        "subject": "ex.employee@acme.local",
        "type": "portability",
        "identity_verified": True,
        "deadline_days": 30,
    },
    {
        "subject": "former.user@umbrella.local",
        "type": "rectification",
        "identity_verified": False,
        "deadline_days": 30,
    },
]

DEMO_RTBF = [
    {
        "subject": "leaving.user@wayne.local",
        "justification": "Employee left organization, requested erasure",
        "scope": {"profile": True, "messages": True, "files": True},
    },
    {
        "subject": "test.account@wayne.local",
        "justification": "Test account cleanup",
        "scope": {"profile": True, "messages": False, "files": False},
    },
]

DEMO_CLASSIFICATION_RULES = [
    {
        "name": "credit-card-pan",
        "regex": r"\b\d{4}[- ]?\d{4}[- ]?\d{4}[- ]?\d{4}\b",
        "action": "alert",
        "severity": "critical",
    },
    {
        "name": "ssn-us",
        "regex": r"\b\d{3}-\d{2}-\d{4}\b",
        "action": "block",
        "severity": "critical",
    },
    {
        "name": "iban",
        "regex": r"\b[A-Z]{2}\d{2}[A-Z0-9]{1,30}\b",
        "action": "tag",
        "severity": "high",
    },
    {
        "name": "email-address",
        "regex": r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b",
        "action": "tag",
        "severity": "low",
    },
    {
        "name": "saudi-national-id",
        "regex": r"\b[12]\d{9}\b",
        "action": "alert",
        "severity": "critical",
    },
    {
        "name": "passport-number",
        "regex": r"\b[A-Z]\d{8}\b",
        "action": "tag",
        "severity": "high",
    },
]

DEMO_PLUGINS = [
    {
        "slug": "calendar-sync",
        "version": "1.2.0",
        "name": "Calendar Sync",
        "author": "Helen Core",
        "category": "productivity",
        "permissions": ["channels:read", "users:list"],
        "installed": True,
        "enabled": True,
    },
    {
        "slug": "translation-ar-en",
        "version": "0.9.1",
        "name": "Arabic ↔ English Translator",
        "author": "Helen Community",
        "category": "ai",
        "permissions": ["messages:send"],
        "installed": True,
        "enabled": True,
    },
    {
        "slug": "matrix-bridge",
        "version": "2.0.0",
        "name": "Matrix Bridge",
        "author": "Helen Federation Team",
        "category": "bridges",
        "permissions": ["federation:*", "messages:send", "channels:read"],
        "installed": False,
        "enabled": False,
    },
    {
        "slug": "anomaly-detector",
        "version": "1.0.3",
        "name": "Anomaly Detector",
        "author": "Helen Security",
        "category": "security",
        "permissions": ["audit:read", "users:list"],
        "installed": False,
        "enabled": False,
    },
    {
        "slug": "sms-gateway",
        "version": "1.1.0",
        "name": "SMS Gateway",
        "author": "Helen Community",
        "category": "integrations",
        "permissions": ["messages:send"],
        "installed": False,
        "enabled": False,
    },
]

DEMO_RETENTION_POLICIES = [
    {"resource_type": "messages", "period_days": 365, "action": "archive"},
    {"resource_type": "files", "period_days": 730, "action": "archive"},
    {"resource_type": "calls_recordings", "period_days": 90, "action": "delete"},
    {"resource_type": "audit_chain", "period_days": 2555, "action": "archive"},
    {"resource_type": "presence_events", "period_days": 30, "action": "delete"},
]


# ---------------------------------------------------------------------------
# Seeders (best-effort, lazy DB import)
# ---------------------------------------------------------------------------


async def _try_db_session():
    """Attempt to acquire an async DB session. Returns None on failure."""
    try:
        from app.db.session import async_session_factory  # type: ignore

        return async_session_factory()
    except Exception as exc:  # pragma: no cover - environment-dependent
        log.warning("DB session unavailable (%s); falling back to JSON snapshot mode", exc)
        return None


def _snapshot_path(scope: str) -> Path:
    out = _PROJECT_ROOT / "data" / "seed_snapshots"
    out.mkdir(parents=True, exist_ok=True)
    return out / f"seed_{scope}.json"


def _write_snapshot(scope: str, payload: List[Dict[str, Any]]) -> None:
    path = _snapshot_path(scope)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    log.info("snapshot[%s] written → %s", scope, path)


async def seed_tenancy(ctx: SeedContext) -> SeedResult:
    res = SeedResult(scope="tenancy")
    items = list(DEMO_TENANTS) + [{"workspace": w} for w in DEMO_WORKSPACES] + [{"user": u} for u in DEMO_USERS]
    if ctx.dry_run:
        log.info("[DRY] tenancy: would create %d items", len(items))
        return res
    _write_snapshot("tenancy", items)
    res.created = len(items)
    log.info("tenancy: seeded %d items (3 tenants + 6 workspaces + 10 users)", res.created)
    return res


async def seed_billing(ctx: SeedContext) -> SeedResult:
    res = SeedResult(scope="billing")
    items = (
        [{"plan": p} for p in DEMO_PLANS]
        + [{"license": _build_license(lic)} for lic in DEMO_LICENSES]
    )
    if ctx.dry_run:
        log.info("[DRY] billing: would create %d items", len(items))
        return res
    _write_snapshot("billing", items)
    res.created = len(items)
    log.info("billing: seeded %d items (3 plans + 2 licenses)", res.created)
    return res


def _build_license(lic: Dict[str, Any]) -> Dict[str, Any]:
    key = secrets.token_urlsafe(24).upper().replace("_", "").replace("-", "")[:32]
    grouped = "-".join(key[i : i + 8] for i in range(0, 32, 8))
    issued = now()
    expires = issued + timedelta(days=lic["duration_days"])
    payload = {
        "key": grouped,
        "tenant_slug": lic["tenant_slug"],
        "plan": lic["plan"],
        "seats": lic["seats"],
        "features": lic["features"],
        "issued_at": issued.isoformat(),
        "expires_at": expires.isoformat(),
        "status": "active",
    }
    return payload


async def seed_dr(ctx: SeedContext) -> SeedResult:
    res = SeedResult(scope="dr")
    backups: List[Dict[str, Any]] = []
    for i, policy in enumerate(DEMO_BACKUP_POLICIES):
        for d in range(5):
            backups.append(
                {
                    "id": deterministic_id("backup", policy["name"], d),
                    "policy": policy["name"],
                    "destination": DEMO_DR_DESTINATIONS[d % len(DEMO_DR_DESTINATIONS)]["name"],
                    "started_at": (now() - timedelta(days=d, hours=i)).isoformat(),
                    "size_bytes": 1024 * 1024 * (500 + d * 100 + i * 50),
                    "status": "succeeded" if d > 0 else "verifying",
                    "integrity_verified": d % 2 == 0,
                }
            )
    drill = {
        "id": deterministic_id("drill", "quarterly", "2026-Q2"),
        "scope": "sandbox",
        "started_at": (now() - timedelta(days=7)).isoformat(),
        "rto_measured_seconds": 1820,
        "rpo_measured_seconds": 86400,
        "result": "pass",
    }
    payload: List[Dict[str, Any]] = (
        [{"destination": d} for d in DEMO_DR_DESTINATIONS]
        + [{"policy": p} for p in DEMO_BACKUP_POLICIES]
        + [{"backup": b} for b in backups]
        + [{"drill": drill}]
    )
    if ctx.dry_run:
        log.info("[DRY] dr: would create %d items", len(payload))
        return res
    _write_snapshot("dr", payload)
    res.created = len(payload)
    log.info("dr: seeded %d items (3 destinations + 3 policies + %d backups + 1 drill)", res.created, len(backups))
    return res


async def seed_federation(ctx: SeedContext) -> SeedResult:
    res = SeedResult(scope="federation")
    shaper_rules = [
        {"peer": p["hostname"], "in_kbps": 100000, "out_kbps": 100000, "burst_kbps": 200000, "priority": 5}
        for p in DEMO_FEDERATION_PEERS
    ]
    payload: List[Dict[str, Any]] = (
        [{"peer": p} for p in DEMO_FEDERATION_PEERS]
        + [{"shaper_rule": s} for s in shaper_rules]
    )
    if ctx.dry_run:
        log.info("[DRY] federation: would create %d items", len(payload))
        return res
    _write_snapshot("federation", payload)
    res.created = len(payload)
    log.info("federation: seeded %d items (4 peers + 4 shaper rules)", res.created)
    return res


async def seed_compliance(ctx: SeedContext) -> SeedResult:
    res = SeedResult(scope="compliance")
    payload: List[Dict[str, Any]] = (
        [{"hold": h} for h in DEMO_LEGAL_HOLDS]
        + [{"dsar": d} for d in DEMO_DSAR]
        + [{"rtbf": r} for r in DEMO_RTBF]
        + [{"classification_rule": c} for c in DEMO_CLASSIFICATION_RULES]
        + [{"retention": rp} for rp in DEMO_RETENTION_POLICIES]
    )
    if ctx.dry_run:
        log.info("[DRY] compliance: would create %d items", len(payload))
        return res
    _write_snapshot("compliance", payload)
    res.created = len(payload)
    log.info(
        "compliance: seeded %d items (2 holds + 3 DSAR + 2 RTBF + 6 rules + 5 retention policies)",
        res.created,
    )
    return res


async def seed_plugins(ctx: SeedContext) -> SeedResult:
    res = SeedResult(scope="plugins")
    payload = [{"plugin": p} for p in DEMO_PLUGINS]
    if ctx.dry_run:
        log.info("[DRY] plugins: would create %d items", len(payload))
        return res
    _write_snapshot("plugins", payload)
    res.created = len(payload)
    installed = sum(1 for p in DEMO_PLUGINS if p["installed"])
    log.info("plugins: seeded %d (installed=%d, catalog=%d)", res.created, installed, res.created - installed)
    return res


async def seed_audit(ctx: SeedContext) -> SeedResult:
    res = SeedResult(scope="audit")
    rules = [
        {
            "name": "5+ failed logins from same IP in 60s",
            "condition_dsl": "action='auth.login_failed' WITHIN 60s",
            "severity": "warn",
            "channels": ["local", "webhook"],
        },
        {
            "name": "Role grant outside business hours",
            "condition_dsl": "action='rbac.role_granted' AND severity>='warn'",
            "severity": "warn",
            "channels": ["local"],
        },
        {
            "name": "Delete by non-admin",
            "condition_dsl": "action IN ['message.delete_any', 'file.delete_any'] AND actor_role != 'admin'",
            "severity": "critical",
            "channels": ["local", "email"],
        },
        {
            "name": "Federation handshake failure",
            "condition_dsl": "action='federation.handshake_failed'",
            "severity": "warn",
            "channels": ["local"],
        },
        {
            "name": "Plugin install with critical permissions",
            "condition_dsl": "action='plugin.install' AND severity='critical'",
            "severity": "warn",
            "channels": ["local", "webhook"],
        },
        {
            "name": "RTBF executed",
            "condition_dsl": "action='compliance.rtbf_executed'",
            "severity": "info",
            "channels": ["local"],
        },
    ]
    payload = [{"alert_rule": r} for r in rules]
    if ctx.dry_run:
        log.info("[DRY] audit: would create %d items", len(payload))
        return res
    _write_snapshot("audit", payload)
    res.created = len(payload)
    log.info("audit: seeded %d alert rules", res.created)
    return res


async def seed_onboarding(ctx: SeedContext) -> SeedResult:
    res = SeedResult(scope="onboarding")
    state = {
        "current_step": 14,
        "completed_steps": list(range(1, 15)),
        "locked": True,
        "finalized_at": now().isoformat(),
        "operator_email": "operator@helen.local",
    }
    if ctx.dry_run:
        log.info("[DRY] onboarding: would set demo state")
        return res
    _write_snapshot("onboarding", [{"state": state}])
    res.created = 1
    log.info("onboarding: seeded finalized demo state")
    return res


SEEDERS: Dict[str, Callable[[SeedContext], Awaitable[SeedResult]]] = {
    "tenancy": seed_tenancy,
    "billing": seed_billing,
    "dr": seed_dr,
    "federation": seed_federation,
    "compliance": seed_compliance,
    "plugins": seed_plugins,
    "audit": seed_audit,
    "onboarding": seed_onboarding,
}


# ---------------------------------------------------------------------------
# Reset helpers
# ---------------------------------------------------------------------------


async def reset_snapshots(scopes: Sequence[str]) -> None:
    for scope in scopes:
        path = _snapshot_path(scope)
        if path.exists():
            path.unlink()
            log.info("reset: removed %s", path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="seed_admin_panels", description=__doc__)
    p.add_argument("--reset", action="store_true", help="Wipe existing snapshots before seeding")
    p.add_argument("--update", action="store_true", help="Upsert (idempotent default behaviour)")
    p.add_argument("--dry-run", action="store_true", help="Print the plan without writing")
    p.add_argument(
        "--scope",
        action="append",
        choices=list(SCOPES) + ["all"],
        help="Limit to one or more scopes (repeatable)",
    )
    p.add_argument("--list-scopes", action="store_true", help="List available scopes and exit")
    return p.parse_args(argv)


async def run(ctx: SeedContext) -> int:
    if ctx.reset:
        await reset_snapshots(ctx.scopes)

    overall = SeedResult(scope="all")
    for scope in ctx.scopes:
        seeder = SEEDERS.get(scope)
        if not seeder:
            log.warning("unknown scope: %s", scope)
            continue
        log.info("=== seeding %s ===", scope)
        try:
            result = await seeder(ctx)
            overall.merge(result)
        except Exception as exc:  # pragma: no cover
            log.exception("scope %s failed", scope)
            overall.add_error(f"{scope}: {exc}")

    log.info(
        "DONE. created=%d updated=%d skipped=%d errors=%d",
        overall.created,
        overall.updated,
        overall.skipped,
        len(overall.errors),
    )
    if overall.errors:
        for e in overall.errors:
            log.error("  %s", e)
        return 1
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    if args.list_scopes:
        for s in SCOPES:
            print(s)
        return 0

    scopes = args.scope or ["all"]
    if "all" in scopes:
        scopes = list(SCOPES)

    ctx = SeedContext(
        dry_run=args.dry_run,
        reset=args.reset,
        update=args.update,
        scopes=scopes,
    )

    try:
        return asyncio.run(run(ctx))
    except KeyboardInterrupt:
        log.warning("interrupted by user")
        return 130


if __name__ == "__main__":
    sys.exit(main())
