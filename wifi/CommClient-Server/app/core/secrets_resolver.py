"""
JWT Unified Secret Resolver — Connectivity Hotfix Layer (Module B).

Why this module exists
----------------------
The Helen server can pull its JWT signing secret from any of four
disjoint sources:

  1. ``data/.secrets.json``               (persistent — written by
                                           ``app.core.persistent_secrets``).
  2. Process environment variables        (``HELEN_JWT_SECRET`` /
                                           ``JWT_SECRET``).
  3. The ``.env`` file at project root    (consumed by pydantic-settings).
  4. ``secrets.token_hex(32)`` generated  (last-resort, in-memory only).

Historically that ladder was implemented inline in three different
places, with subtly different fallback semantics. The result: after an
upgrade, the secret read by the FastAPI app sometimes diverged from the
secret read by the worker pool. Every issued JWT was instantly invalid
for the side that didn't read the file. This module composes the
existing :mod:`app.core.persistent_secrets` machinery into one
deterministic resolver with strict priority:

   .secrets.json   →   HELEN_JWT_SECRET env   →   .env (JWT_SECRET)
                   →   generate + persist new

Public API
----------
:func:`resolve_jwt_secret`
    Main entrypoint. Returns the highest-priority secret, persisting a
    freshly-generated one when every source is empty.

:func:`sync_secrets`
    Pulls from all sources, picks the highest priority, writes it to a
    target file so every dependent process can rely on the same secret.

:func:`verify_consistency`
    Returns a mismatch report. ``ok=False`` means at least two sources
    disagree and the operator must decide which one is canonical.

CLI
---
The module ships an ``argparse`` entry point so the server scripts can
run it as a one-shot step::

    python -m app.core.secrets_resolver --show
    python -m app.core.secrets_resolver --sync
    python -m app.core.secrets_resolver --verify

Logging
-------
Uses :mod:`structlog` (the project-wide logger). When structlog has not
been configured yet (e.g. running from CLI before
``setup_logging()``), the calls are no-ops — structlog tolerates that.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

# Compose, do not duplicate: reuse the existing persistent-secrets module
# for path resolution, file hardening, and atomic writes.
from app.core.persistent_secrets import (
    _atomic_write,
    _harden_permissions,
    _read_existing,
    _resolve_data_dir,
    secrets_file_path,
)

log = structlog.get_logger("secrets_resolver")

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

# Number of entropy bytes fed into ``secrets.token_urlsafe`` for new secrets.
# 64 bytes → ~86 URL-safe characters → ~512 bits of entropy.
_JWT_SECRET_BYTES = 64

# Minimum acceptable secret length. Anything shorter is treated as missing
# (defends against accidental empty values in ``.env``).
_MIN_SECRET_LEN = 16

# Environment variable names, in priority order.
_ENV_KEYS_PRIMARY = ("HELEN_JWT_SECRET",)
_ENV_KEYS_FALLBACK = ("JWT_SECRET",)

# Default name of the persistent secrets file.
_DEFAULT_PERSIST_NAME = ".secrets.json"

# JSON key under which the JWT secret is stored inside the secrets file.
_JSON_KEY = "jwt_secret"


# ─────────────────────────────────────────────────────────────────────────────
# Data types
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SourceSnapshot:
    """A single source's contribution to the resolution result."""

    source: str
    secret: str | None
    path: str | None = None

    @property
    def present(self) -> bool:
        return bool(self.secret) and len(self.secret or "") >= _MIN_SECRET_LEN


# ─────────────────────────────────────────────────────────────────────────────
# Source loaders
# ─────────────────────────────────────────────────────────────────────────────


def _load_persistent_secret() -> str | None:
    """Read JWT secret from ``<data_dir>/.secrets.json``.

    Wraps :func:`app.core.persistent_secrets._read_existing` so we
    benefit from the same hardening / atomic-write contract used at
    server boot.
    """
    path = secrets_file_path()
    data = _read_existing(path)
    if not isinstance(data, dict):
        return None
    val = data.get(_JSON_KEY)
    if isinstance(val, str) and len(val) >= _MIN_SECRET_LEN:
        return val
    return None


def _load_env_secret() -> str | None:
    """Read JWT secret from the process environment.

    Honours the canonical key ``HELEN_JWT_SECRET`` first, falling back
    to legacy ``JWT_SECRET`` for compatibility with older operator
    runbooks.
    """
    for key in _ENV_KEYS_PRIMARY + _ENV_KEYS_FALLBACK:
        val = os.environ.get(key)
        if val and len(val) >= _MIN_SECRET_LEN:
            return val
    return None


def _project_root() -> Path:
    """Best-effort project-root resolution for the ``.env`` lookup.

    Mirrors the calculation in :class:`app.core.config.Settings`:
    three levels up from ``app/core/secrets_resolver.py`` lands on the
    repo root.
    """
    return Path(__file__).resolve().parent.parent.parent


def _load_dotenv_secret() -> str | None:
    """Read JWT_SECRET from the project's ``.env`` file (if any).

    This is a deliberately minimal parser — we only look for
    ``JWT_SECRET=<value>`` lines, ignoring quotes, comments, and
    interpolation. Keeping it parser-free avoids pulling in
    ``python-dotenv`` at this layer.
    """
    env_path = _project_root() / ".env"
    if not env_path.is_file():
        return None
    try:
        for raw in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key in ("HELEN_JWT_SECRET", "JWT_SECRET") and len(value) >= _MIN_SECRET_LEN:
                return value
    except OSError as exc:
        log.warning("dotenv_read_failed", path=str(env_path), error=str(exc))
    return None


def _generate_and_persist() -> str:
    """Generate a fresh secret and write it to ``.secrets.json``.

    Used only when every other source is empty. Mirrors the entropy
    settings of :mod:`app.core.persistent_secrets`.
    """
    new_secret = secrets.token_urlsafe(_JWT_SECRET_BYTES)
    path = secrets_file_path()
    existing = _read_existing(path) or {}
    if not isinstance(existing, dict):
        existing = {}
    existing[_JSON_KEY] = new_secret
    existing.setdefault("version", 1)
    try:
        _atomic_write(path, existing)
        _harden_permissions(path)
    except OSError as exc:
        log.error("generate_persist_failed", path=str(path), error=str(exc))
    log.info("generated_new_jwt_secret", path=str(path))
    return new_secret


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────


def collect_snapshots() -> list[SourceSnapshot]:
    """Return every source's snapshot, in priority order.

    Useful for diagnostics and for :func:`verify_consistency`.
    """
    persistent_path = secrets_file_path()
    snapshots: list[SourceSnapshot] = [
        SourceSnapshot(
            source="persistent",
            secret=_load_persistent_secret(),
            path=str(persistent_path),
        ),
        SourceSnapshot(source="env", secret=_load_env_secret(), path=None),
        SourceSnapshot(
            source="dotenv",
            secret=_load_dotenv_secret(),
            path=str(_project_root() / ".env"),
        ),
    ]
    return snapshots


def resolve_jwt_secret() -> str:
    """Resolve the JWT secret using the priority chain.

    Priority (highest first):

        1. ``data/.secrets.json``  (canonical persistent store)
        2. ``HELEN_JWT_SECRET``    (operator override)
        3. ``.env``                (developer convenience)
        4. Newly generated         (auto-bootstrap)

    Returns
    -------
    str
        The resolved secret. Always at least ``_MIN_SECRET_LEN`` chars.
    """
    for snap in collect_snapshots():
        if snap.present and snap.secret is not None:
            log.debug("jwt_secret_resolved", source=snap.source)
            return snap.secret
    return _generate_and_persist()


def sync_secrets(target: str = _DEFAULT_PERSIST_NAME) -> dict[str, Any]:
    """Sync the canonical secret to ``target``.

    Picks the highest-priority non-empty source, writes it to the
    persistent file (resolved against the data dir if ``target`` is a
    bare filename), and returns a report describing what happened.

    Parameters
    ----------
    target:
        Either a bare filename (resolved against the data dir) or an
        absolute path. Defaults to ``.secrets.json``.

    Returns
    -------
    dict
        Diagnostic payload — ``{ "written": bool, "path": str,
        "source": str, "generated": bool, "secret_preview": str }``.
    """
    target_path = Path(target)
    if not target_path.is_absolute():
        target_path = _resolve_data_dir() / target_path

    snapshots = collect_snapshots()
    winner: SourceSnapshot | None = next(
        (s for s in snapshots if s.present and s.secret is not None), None
    )
    generated = False
    if winner is None:
        winner = SourceSnapshot(
            source="generated",
            secret=secrets.token_urlsafe(_JWT_SECRET_BYTES),
            path=str(target_path),
        )
        generated = True

    payload = _read_existing(target_path) or {}
    if not isinstance(payload, dict):
        payload = {}
    payload[_JSON_KEY] = winner.secret
    payload.setdefault("version", 1)

    written = False
    try:
        _atomic_write(target_path, payload)
        _harden_permissions(target_path)
        written = True
    except OSError as exc:
        log.error("sync_write_failed", path=str(target_path), error=str(exc))

    # Propagate to env so downstream modules in the same process pick it
    # up immediately. We do not overwrite an operator-set value because
    # that would defeat the point of the override.
    if not os.environ.get(_ENV_KEYS_PRIMARY[0]) and winner.secret is not None:
        os.environ[_ENV_KEYS_PRIMARY[0]] = winner.secret
    if not os.environ.get(_ENV_KEYS_FALLBACK[0]) and winner.secret is not None:
        os.environ[_ENV_KEYS_FALLBACK[0]] = winner.secret

    secret_preview = ""
    if winner.secret is not None:
        secret_preview = winner.secret[:6] + "…" + winner.secret[-4:]

    log.info(
        "sync_secrets",
        source=winner.source,
        path=str(target_path),
        written=written,
        generated=generated,
    )

    return {
        "written": written,
        "path": str(target_path),
        "source": winner.source,
        "generated": generated,
        "secret_preview": secret_preview,
    }


def verify_consistency() -> dict[str, bool]:
    """Compare every source and report mismatches.

    The returned dict carries one boolean per source pair and an
    aggregate ``ok`` flag::

        {
          "persistent_vs_env": True,
          "persistent_vs_dotenv": False,
          "env_vs_dotenv": False,
          "ok": False,
        }

    ``ok`` is True iff every present source carries the same value.
    Sources that are missing are skipped (i.e. a missing source never
    causes ``ok=False``).
    """
    snaps = {s.source: s for s in collect_snapshots()}
    present = {name: s.secret for name, s in snaps.items() if s.present and s.secret}

    report: dict[str, bool] = {}
    pairs = [
        ("persistent", "env"),
        ("persistent", "dotenv"),
        ("env", "dotenv"),
    ]
    for a, b in pairs:
        key = f"{a}_vs_{b}"
        if a in present and b in present:
            report[key] = present[a] == present[b]
        else:
            # Missing source can't mismatch — treat as agreement.
            report[key] = True

    report["ok"] = all(report[k] for k in report)
    log.info("verify_consistency", **report)
    return report


# ─────────────────────────────────────────────────────────────────────────────
# CLI entry
# ─────────────────────────────────────────────────────────────────────────────


def _redact(secret: str | None) -> str:
    if not secret:
        return "<none>"
    if len(secret) <= 12:
        return "<short>"
    return secret[:6] + "…" + secret[-4:]


def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.core.secrets_resolver",
        description="Resolve, sync, and verify the Helen JWT signing secret.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--show", action="store_true",
        help="Print the resolved secret (redacted) and every source snapshot.",
    )
    group.add_argument(
        "--sync", action="store_true",
        help="Pick the highest-priority secret and write it to .secrets.json.",
    )
    group.add_argument(
        "--verify", action="store_true",
        help="Compare sources and exit 0 on agreement, 1 on mismatch.",
    )
    parser.add_argument(
        "--target", default=_DEFAULT_PERSIST_NAME,
        help="Override the file written by --sync (default: .secrets.json).",
    )
    args = parser.parse_args(argv)

    if args.show:
        snaps = collect_snapshots()
        report = {
            "resolved": _redact(resolve_jwt_secret()),
            "sources": [
                {
                    "source": s.source,
                    "present": s.present,
                    "secret_preview": _redact(s.secret) if s.present else "<none>",
                    "path": s.path,
                }
                for s in snaps
            ],
        }
        print(json.dumps(report, indent=2))
        return 0

    if args.sync:
        result = sync_secrets(target=args.target)
        print(json.dumps(result, indent=2))
        return 0 if result["written"] else 2

    if args.verify:
        result = verify_consistency()
        print(json.dumps(result, indent=2))
        return 0 if result["ok"] else 1

    # argparse should make this unreachable.
    return 2  # pragma: no cover


__all__ = [
    "SourceSnapshot",
    "collect_snapshots",
    "resolve_jwt_secret",
    "sync_secrets",
    "verify_consistency",
    "_load_dotenv_secret",
    "_load_env_secret",
    "_load_persistent_secret",
    "_generate_and_persist",
]


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_cli())
