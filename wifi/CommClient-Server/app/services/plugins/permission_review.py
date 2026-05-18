"""
Phase 7 / Module AH — Plugin Permission Severity Mapper
========================================================

Permissions are not equally dangerous. The marketplace UI needs a
severity hint so the operator can make a sane consent decision, and
the installer must **block** an install with high/critical permissions
unless the operator explicitly accepts them in the request body.

Severity ladder (lowest → highest):

* ``low``      — read-only, scope-bounded (e.g. own messages).
* ``medium``   — write own scope, list users in workspace.
* ``high``     — write across users/channels, delete files, federation.
* ``critical`` — admin-tier (RBAC bypass, audit forging, webhook abuse).

Permission codes follow the ``area:verb`` convention. ``admin:*``,
``audit:*``, ``federation:*``, ``webhooks:*`` map to ``critical``.

The existing :data:`app.services.plugins.manifest_schema.ALLOWED_PERMISSIONS`
list uses a different (legacy) dotted style; we accept BOTH the
``area:verb`` and ``area.verb`` forms transparently.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from app.core.logging import get_logger

logger = get_logger(__name__)


Severity = str  # "low" | "medium" | "high" | "critical"

SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}


# Canonical map. Glob-prefix wins last → first if both match.
_PERMISSION_TABLE: dict[str, tuple[Severity, str]] = {
    # ── channels ────────────────────────────────────────────────────
    "channels:read":           ("low",     "Read channel list and metadata"),
    "channels:write":          ("high",    "Create / rename / archive channels"),
    "channels.read":           ("low",     "Read channel list and metadata"),
    "channels.create":         ("medium",  "Create new channels"),
    # ── messages ────────────────────────────────────────────────────
    "messages:read":           ("low",     "Read messages visible to plugin"),
    "messages:send":           ("medium",  "Send messages as the plugin"),
    "messages:edit_any":       ("high",    "Edit any user's messages"),
    "messages:delete_any":     ("high",    "Delete any user's messages"),
    "messages.read":           ("low",     "Read messages visible to plugin"),
    "messages.send":           ("medium",  "Send messages as the plugin"),
    "messages.delete":         ("high",    "Delete messages"),
    # ── files ───────────────────────────────────────────────────────
    "files:read":              ("medium",  "Read files in workspace"),
    "files:write":             ("high",    "Upload / replace files"),
    "files:delete":            ("high",    "Delete files in workspace"),
    "files.read":              ("medium",  "Read files in workspace"),
    "files.upload":            ("high",    "Upload files"),
    # ── users ───────────────────────────────────────────────────────
    "users:list":              ("low",     "List workspace users"),
    "users:read":              ("low",     "Read user profile data"),
    "users:update_any":        ("critical", "Modify any user account"),
    "users.read":              ("low",     "Read user profile data"),
    # ── kv / sdk basics ─────────────────────────────────────────────
    "kv:read":                 ("low",     "Read plugin private KV store"),
    "kv:write":                ("low",     "Write plugin private KV store"),
    "kv.read":                 ("low",     "Read plugin private KV store"),
    "kv.write":                ("low",     "Write plugin private KV store"),
    "http:outbound":           ("medium",  "Outbound HTTP to allowlisted hosts"),
    "http.outbound":           ("medium",  "Outbound HTTP to allowlisted hosts"),
    # ── audit / admin ───────────────────────────────────────────────
    "audit:read":              ("high",    "Read audit log entries"),
    "audit:write":             ("critical", "Append audit entries"),
    "admin:*":                 ("critical", "Full admin / RBAC bypass"),
    "admin:users":             ("critical", "Manage user accounts"),
    "admin:rbac":              ("critical", "Modify roles and permissions"),
    "admin:config":            ("critical", "Modify server configuration"),
    # ── federation / webhooks ───────────────────────────────────────
    "federation:*":            ("critical", "Federation control plane"),
    "federation:read":         ("high",    "Read remote-node state"),
    "federation:write":        ("critical", "Modify federation peers"),
    "webhooks:*":              ("critical", "Manage outbound webhooks"),
    "webhooks:read":           ("medium",  "List configured webhooks"),
    "webhooks:write":          ("high",    "Create / modify webhooks"),
    # ── calls / agents ──────────────────────────────────────────────
    "calls.read":              ("medium",  "Read call metadata"),
    "agents.read":             ("low",     "Read AI agent definitions"),
    "agents.invoke":           ("medium",  "Invoke AI agents"),
    "workspace.read":          ("low",     "Read workspace metadata"),
}


_WILDCARD_TABLE: list[tuple[str, Severity]] = [
    ("admin:",       "critical"),
    ("audit:",       "critical"),
    ("federation:",  "critical"),
    ("webhooks:",    "high"),
    ("files:",       "high"),
    ("users:",       "high"),
    ("messages:",    "medium"),
    ("channels:",    "medium"),
    ("kv:",          "low"),
    ("http:",        "medium"),
]


@dataclass
class PermissionInfo:
    code: str
    severity: Severity
    description: str
    requires_explicit_accept: bool


# ───────────────────────────────────────────────────────────────────────
# Mapper
# ───────────────────────────────────────────────────────────────────────


class PermissionReview:
    """Permission review + blocking gate for installs."""

    HIGH_OR_ABOVE = ("high", "critical")

    def severity(self, code: str) -> Severity:
        if not code:
            return "low"
        if code in _PERMISSION_TABLE:
            return _PERMISSION_TABLE[code][0]
        # Glob "area:*"
        if code.endswith(":*"):
            return _PERMISSION_TABLE.get(code, ("critical", ""))[0]
        for prefix, sev in _WILDCARD_TABLE:
            if code.startswith(prefix):
                return sev
        return "medium"   # unknown but namespaced → assume medium

    def describe(self, code: str) -> str:
        return _PERMISSION_TABLE.get(code, (None, code))[1] or code

    def info(self, code: str) -> PermissionInfo:
        sev = self.severity(code)
        return PermissionInfo(
            code=code, severity=sev,
            description=self.describe(code),
            requires_explicit_accept=sev in self.HIGH_OR_ABOVE,
        )

    def review(self, codes: Iterable[str]) -> list[PermissionInfo]:
        seen: set[str] = set()
        out: list[PermissionInfo] = []
        for c in codes:
            if not c or c in seen:
                continue
            seen.add(c)
            out.append(self.info(c))
        # Sort highest → lowest
        out.sort(key=lambda p: -SEVERITY_ORDER.get(p.severity, 0))
        return out

    def summary(self, codes: Iterable[str]) -> dict[str, int]:
        counts = {"low": 0, "medium": 0, "high": 0, "critical": 0}
        for info in self.review(codes):
            counts[info.severity] = counts.get(info.severity, 0) + 1
        return counts

    def highest(self, codes: Iterable[str]) -> Severity:
        sev = "low"
        cur = 0
        for c in codes:
            v = SEVERITY_ORDER.get(self.severity(c), 0)
            if v > cur:
                cur = v
                sev = self.severity(c)
        return sev

    # -----------------------------------------------------------------
    # Install gating
    # -----------------------------------------------------------------

    def must_accept(self, codes: Iterable[str]) -> list[str]:
        """Return the subset of permissions that requires explicit accept."""
        out: list[str] = []
        for c in codes:
            if self.severity(c) in self.HIGH_OR_ABOVE:
                out.append(c)
        return out

    def gate_install(
        self,
        requested: Iterable[str],
        *,
        accepted: bool,
        explicitly_accepted: Iterable[str] | None,
    ) -> tuple[bool, str, list[str]]:
        """Return ``(allowed, reason, missing_explicit)``.

        Rules:

        * If no high/critical perms → ``accepted=True`` is enough.
        * If high/critical perms exist:
            - ``accepted`` must be True.
            - **Every** high/critical perm must appear in
              ``explicitly_accepted``.
        """
        requested_list = list(requested or [])
        if not accepted:
            return False, "permissions-not-accepted", []
        must = self.must_accept(requested_list)
        if not must:
            return True, "no-high-perms", []
        explicit = set(explicitly_accepted or [])
        missing = [p for p in must if p not in explicit]
        if missing:
            return (
                False,
                "high-perms-not-explicitly-accepted",
                missing,
            )
        return True, "ok", []


_default = PermissionReview()


def review_permissions(codes: Iterable[str]) -> list[PermissionInfo]:
    return _default.review(codes)


def gate_install(
    codes: Iterable[str],
    *,
    accepted: bool,
    explicitly_accepted: Iterable[str] | None,
) -> tuple[bool, str, list[str]]:
    return _default.gate_install(
        codes, accepted=accepted,
        explicitly_accepted=explicitly_accepted,
    )


__all__ = [
    "PermissionReview", "PermissionInfo", "Severity",
    "review_permissions", "gate_install",
]
