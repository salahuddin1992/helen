"""
app.domains — Phase 4 Module R: Domain-Oriented Re-Export Facade
================================================================

This package exposes a CLEAN, DOMAIN-ORIENTED import surface on top of the
existing 617-file ``app/`` tree. It does NOT implement anything new — every
symbol is re-exported from an existing module via a safe-import helper that
silently skips missing optionals (e.g., Phase-2/3 modules that may not be
installed in a particular deployment profile).

Why this exists
---------------
Before this facade, new code had to know the exact internal path of every
service / model / route::

    from app.api.routes.auth import router as auth_router
    from app.services.auth_service import authenticate_user
    from app.models.user import User
    from app.core.security import create_access_token, verify_token

That coupling makes refactors painful and obscures the actual domain
boundaries. With the facade, the same code reads::

    from app.domains import auth
    auth.router
    auth.authenticate_user
    auth.User
    auth.create_access_token

The lookup is done ONCE at import time. The cost is one cheap dict-update
per domain, and import errors in optional modules are swallowed (with a
DEBUG-level log line for forensic inspection).

Versioning
----------
The facade follows semver-ish rules. Adding a new symbol is a MINOR bump;
removing a symbol is a MAJOR bump. Domain renames are MAJOR. The current
version constant lives in ``DOMAINS_VERSION`` below.

Public surface
--------------
``__all__`` exposes the list of domain names. Each domain is a sub-package
with its own ``__all__`` listing the re-exported symbol names. Use the
``_registry`` module for runtime introspection (admin UI, health check,
etc).
"""

from __future__ import annotations

# Version of the facade itself — NOT the version of the underlying app.
DOMAINS_VERSION = "4.0.0"
PHASE = "Phase 4 — Performance & Hardening"

# Canonical ordered list of domain names. Order matters for introspection
# UIs (Admin → Domains panel) — list in dependency order, not alphabetical.
DOMAIN_NAMES = (
    "system",       # config, audit, crypto, secrets, backup, monitoring
    "auth",         # users, jwt, sessions
    "rbac",         # roles, permissions, enforcer
    "tenancy",      # workspaces, multi-tenancy
    "messaging",    # channels, messages, drafts
    "files",        # uploads, resumable, acceptance
    "calls",        # signaling, sfu, recording
    "realtime",     # socket.io, p2p, overlay
    "federation",   # cluster, gossip, cross-cluster
    "oauth",        # external IdPs
    "agents",       # bot / programmatic agents
    "pairing",      # device pairing v1/v2
    "admin",        # admin-only routers
)

__all__ = list(DOMAIN_NAMES) + ["DOMAINS_VERSION", "PHASE", "DOMAIN_NAMES"]


def list_domains() -> list[str]:
    """Return the canonical domain names. Cheap wrapper for non-Python callers."""
    return list(DOMAIN_NAMES)


def get_domain_info() -> dict:
    """Return a metadata dict suitable for admin-UI consumption."""
    return {
        "version": DOMAINS_VERSION,
        "phase": PHASE,
        "domains": list(DOMAIN_NAMES),
        "count": len(DOMAIN_NAMES),
    }
