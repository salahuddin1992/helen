"""
Phase 6 / Module AE — Dependency vulnerability scanner.

Runs daily, parses ``requirements.txt``, looks up every package on
OSV.dev (https://api.osv.dev/v1/query), and persists matched
advisories into ``security_advisories``.

If httpx is missing the scanner is a no-op. If OSV is unreachable we
log and retry next cycle.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.session import async_session_factory
from app.models.security import SecurityAdvisory, SecurityEvent
from app.observability.metrics_exporter import counter_inc

logger = get_logger(__name__)


OSV_ENDPOINT = "https://api.osv.dev/v1/query"
DAILY_INTERVAL_SECONDS = 24 * 3600

_REQ_RE = re.compile(
    r"^\s*([A-Za-z0-9_\-\.]+)\s*(?:\[[^\]]+\])?\s*(?:==|>=|~=|>)\s*([A-Za-z0-9\.\-_+]+)"
)


@dataclass
class _Req:
    name: str
    version: str


def _parse_requirements(path: Path) -> list[_Req]:
    out: list[_Req] = []
    if not path.is_file():
        return out
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        s = raw.split("#", 1)[0].strip()
        if not s or s.startswith("-"):
            continue
        m = _REQ_RE.match(s)
        if not m:
            continue
        out.append(_Req(name=m.group(1).lower(), version=m.group(2)))
    return out


class DependencyScanner:
    def __init__(self, requirements_path: Optional[Path] = None) -> None:
        s = get_settings()
        self.path = requirements_path or (s.PROJECT_ROOT / "requirements.txt")
        self._task: Optional[asyncio.Task[None]] = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="dep-scanner")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=2.0)
            except asyncio.TimeoutError:                            # pragma: no cover
                self._task.cancel()

    async def _run(self) -> None:
        # initial delay so app finishes booting
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=120.0)
        except asyncio.TimeoutError:
            pass
        while not self._stop.is_set():
            try:
                count = await self.scan_now()
                logger.info("dep-scanner: scan complete, %d new advisories", count)
            except Exception as exc:                                # pragma: no cover
                logger.warning("dep-scanner: scan err (%s)", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=DAILY_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                continue

    async def scan_now(self) -> int:
        """Run a scan and return the number of NEW advisories found."""
        try:
            import httpx
        except Exception:                                           # pragma: no cover
            logger.warning("dep-scanner: httpx missing — scan skipped")
            return 0
        reqs = _parse_requirements(self.path)
        if not reqs:
            return 0
        new_count = 0
        async with httpx.AsyncClient(timeout=15.0) as cli:
            for req in reqs:
                try:
                    new_count += await self._scan_one(cli, req)
                except Exception as exc:                            # pragma: no cover
                    logger.warning("dep-scanner: %s err (%s)", req.name, exc)
        return new_count

    async def _scan_one(self, cli, req: _Req) -> int:
        body = {
            "version": req.version,
            "package": {"name": req.name, "ecosystem": "PyPI"},
        }
        r = await cli.post(OSV_ENDPOINT, json=body)
        if r.status_code >= 300:                                    # pragma: no cover
            return 0
        data = r.json() or {}
        vulns = data.get("vulns") or []
        if not vulns:
            return 0
        new = 0
        async with async_session_factory() as db:
            for v in vulns:
                cve = self._first_cve(v.get("aliases") or [])
                severity = self._severity(v)
                fixed_in = self._fixed_in(v)
                # dedup: same package+version+cve
                exists = (await db.execute(
                    select(SecurityAdvisory)
                    .where(SecurityAdvisory.package == req.name)
                    .where(SecurityAdvisory.version == req.version)
                    .where(SecurityAdvisory.cve == cve)
                )).scalar_one_or_none()
                if exists is not None:
                    continue
                db.add(SecurityAdvisory(
                    package=req.name, version=req.version, cve=cve,
                    severity=severity,
                    summary=(v.get("summary") or v.get("details") or "")[:1000],
                    fixed_in=fixed_in,
                ))
                db.add(SecurityEvent(
                    kind="advisory_detected",
                    severity=("critical" if severity == "critical"
                              else "warning"),
                    payload={
                        "package": req.name, "version": req.version,
                        "cve": cve, "fixed_in": fixed_in,
                    },
                ))
                new += 1
            try:
                await db.commit()
            except Exception:                                       # pragma: no cover
                await db.rollback()
        if new:
            counter_inc("ids_events_total", kind="advisory", action="detect")
        return new

    @staticmethod
    def _first_cve(aliases: list[str]) -> Optional[str]:
        for a in aliases:
            if a.startswith("CVE-"):
                return a
        return aliases[0] if aliases else None

    @staticmethod
    def _severity(v: dict[str, Any]) -> str:
        # CVSS severity if available
        severities = v.get("severity") or []
        for s in severities:
            score = s.get("score") or ""
            if score:
                try:
                    if "CVSS:3" in score:
                        # heuristic: any score string longer than 5 means severity present
                        pass
                except Exception:                                   # pragma: no cover
                    pass
        dbs = (v.get("database_specific") or {})
        sev = dbs.get("severity")
        if isinstance(sev, str):
            return sev.lower()
        return "unknown"

    @staticmethod
    def _fixed_in(v: dict[str, Any]) -> Optional[str]:
        for affected in v.get("affected") or []:
            for r in affected.get("ranges") or []:
                for ev in r.get("events") or []:
                    if "fixed" in ev:
                        return str(ev["fixed"])
        return None


_singleton: Optional[DependencyScanner] = None


def get_dep_scanner() -> DependencyScanner:
    global _singleton
    if _singleton is None:
        _singleton = DependencyScanner()
    return _singleton
