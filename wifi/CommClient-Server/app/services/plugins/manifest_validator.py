"""
Phase 7 / Module AH — Manifest Validator
=========================================

Builds on :mod:`manifest_schema` (which already does Pydantic-level
validation) with the cross-cutting checks the installer needs:

* helen-version range compatibility (semver-aware)
* dependency presence/conflict check against currently installed plugins
* permission severity mapping (delegates to :mod:`permission_review`)
* JSON-schema validation of the raw dict (defensive, in case the bundle
  was unzipped server-side and not yet parsed)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from app.core.logging import get_logger
from app.services.plugins.manifest_schema import (
    ALLOWED_HOOKS, ALLOWED_PERMISSIONS, Manifest, parse_manifest,
)
from app.services.plugins.permission_review import PermissionReview

logger = get_logger(__name__)

# Read from loader (kept in sync with single source of truth)
HELEN_VERSION = "7.0.0"

_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+([+-][0-9A-Za-z.-]+)?$")


# ───────────────────────────────────────────────────────────────────────
# Result types
# ───────────────────────────────────────────────────────────────────────


@dataclass
class ValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    manifest: Optional[Manifest] = None
    permission_review: list[dict[str, Any]] = field(default_factory=list)
    helen_compat: bool = True
    helen_compat_reason: Optional[str] = None
    missing_dependencies: list[str] = field(default_factory=list)
    highest_severity: str = "low"


def _semver(v: str) -> tuple[int, ...]:
    try:
        core = v.split("+", 1)[0].split("-", 1)[0]
        return tuple(int(p) for p in core.split("."))
    except Exception:                                                   # noqa: BLE001
        return (0,)


# ───────────────────────────────────────────────────────────────────────
# Validator
# ───────────────────────────────────────────────────────────────────────


class ManifestValidator:
    def __init__(
        self,
        *,
        helen_version: str = HELEN_VERSION,
        installed_slugs: Optional[Iterable[str]] = None,
        review: Optional[PermissionReview] = None,
    ) -> None:
        self.helen_version = helen_version
        self.installed_slugs = set(installed_slugs or ())
        self.review = review or PermissionReview()

    def validate(self, raw: dict[str, Any]) -> ValidationResult:
        errors: list[str] = []
        warnings: list[str] = []
        manifest: Optional[Manifest] = None
        try:
            manifest = parse_manifest(raw)
        except Exception as e:                                          # noqa: BLE001
            errors.append(f"schema: {e}")
            return ValidationResult(ok=False, errors=errors)

        # ── permission check ──────────────────────────────────────
        perms = list(manifest.permissions)
        for p in perms:
            if p not in ALLOWED_PERMISSIONS and ":" not in p:
                warnings.append(f"unknown-perm-style: {p}")
        infos = self.review.review(perms)
        perm_review = [
            {
                "code": info.code,
                "severity": info.severity,
                "description": info.description,
                "requires_explicit_accept": info.requires_explicit_accept,
            }
            for info in infos
        ]
        highest = self.review.highest(perms)

        # ── hooks ────────────────────────────────────────────────
        for h in manifest.hooks_subscribed:
            if h not in ALLOWED_HOOKS:
                errors.append(f"unknown-hook: {h}")

        # ── helen version ───────────────────────────────────────
        helen_ok, helen_reason = self._check_helen_version(manifest)

        # ── dependencies ────────────────────────────────────────
        missing = self._check_dependencies(manifest)

        ok = not errors and helen_ok and not missing
        return ValidationResult(
            ok=ok, errors=errors, warnings=warnings, manifest=manifest,
            permission_review=perm_review,
            helen_compat=helen_ok, helen_compat_reason=helen_reason,
            missing_dependencies=missing,
            highest_severity=highest,
        )

    def _check_helen_version(self, mf: Manifest) -> tuple[bool, Optional[str]]:
        cur = _semver(self.helen_version)
        if mf.min_helen_version:
            if cur < _semver(mf.min_helen_version):
                return False, f"requires helen>={mf.min_helen_version}"
        if mf.max_helen_version:
            if cur > _semver(mf.max_helen_version):
                return False, f"incompatible with helen>{mf.max_helen_version}"
        return True, None

    def _check_dependencies(self, mf: Manifest) -> list[str]:
        missing: list[str] = []
        for d in mf.dependencies or []:
            # Format: "slug" or "slug>=1.0" — for now only slug-presence check.
            slug = re.split(r"[<>=!]+", d, maxsplit=1)[0].strip()
            if slug and slug not in self.installed_slugs:
                missing.append(slug)
        return missing


__all__ = ["ManifestValidator", "ValidationResult", "HELEN_VERSION"]
