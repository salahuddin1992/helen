"""
CommClient Server — LAN-only communication platform backend.
"""

__version__ = "1.0.0"

# ── LAN-server hardening (Task #1 + #4) ─────────────────────────────────
# Import the extended_bootstrap module EARLY so its side effects
# (`ensure_persistent_secrets_loaded`) run BEFORE any code evaluates
# `app.core.config.get_settings()`. Without this ordering the random
# `Field(default_factory=...)` would capture a fresh JWT_SECRET on every
# reboot, invalidating every client token.
#
# Failures here are non-fatal — the module itself falls back to in-memory
# secrets and logs to stderr.
try:  # pragma: no cover — import-time side effect
    from app.core import extended_bootstrap as _extended_bootstrap  # noqa: F401
except Exception as _exc:  # pragma: no cover
    import sys as _sys
    print(
        f"[app.__init__] extended_bootstrap import failed: {_exc}",
        file=_sys.stderr,
    )
