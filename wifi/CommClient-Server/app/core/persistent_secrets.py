"""
Persistent secret manager — LAN-server hardening (Task #1).

Purpose
-------
The original `app.core.config.Settings` generates a fresh `JWT_SECRET` on every
startup when the environment does not supply one. In the "PC-as-LAN-server"
deployment topology, the server process is restarted frequently (laptop
sleep/wake, manual restarts, upgrades) and every restart invalidates EVERY
client's access + refresh token. Users would be forced to re-login constantly,
and Socket.IO reconnects would be rejected with `Invalid token`.

This module provides a deterministic, process-boot-stable secret store that:

  * Persists JWT_SECRET and an E2EE master seed to a protected JSON file
    under `%APPDATA%/CommClient/data/.secrets.json` on Windows (respecting
    `COMMCLIENT_DATA_DIR` and `ELECTRON_DATA_DIR` overrides from the Electron
    launcher).
  * Atomically creates the file on first run via `secrets.token_urlsafe(64)`.
  * Exports the secrets into `os.environ` BEFORE `get_settings()` evaluates
    its `Field(default_factory=...)` — so `Settings.JWT_SECRET` picks up the
    persisted value transparently.
  * Hardens file permissions on Windows (`icacls`) and POSIX (`chmod 0600`)
    best-effort.
  * Never touches / never rewrites existing code in config.py — consumers
    just import and call `ensure_persistent_secrets_loaded()` very early.

Thread-safety
-------------
Called once at import time from `app.core.extended_bootstrap`. The function
itself is idempotent — second call is a no-op.
"""

from __future__ import annotations

import json
import os
import secrets
import stat
import sys
import threading
from pathlib import Path
from typing import Any

# We intentionally do NOT import from app.core.logging here — this module
# runs BEFORE logging is configured. Fall back to stderr for errors.

_SECRETS_FILENAME = ".secrets.json"
_SECRETS_VERSION = 1

# Size of each generated secret (bytes of entropy input). token_urlsafe
# returns roughly ceil(n * 4/3) characters.
_JWT_SECRET_BYTES = 64
_E2EE_SEED_BYTES = 64
_WORKER_TOKEN_BYTES = 32

_load_lock = threading.Lock()
_already_loaded = False


# ─────────────────────────────────────────────────────────────────────────────
# Data directory resolution
# ─────────────────────────────────────────────────────────────────────────────


def _resolve_data_dir() -> Path:
    """
    Resolve the directory that should hold `.secrets.json`.

    Priority:
      1. `COMMCLIENT_DATA_DIR`   — injected by Electron launcher.
      2. `ELECTRON_DATA_DIR`     — alternative name supported for legacy.
      3. `%APPDATA%/CommClient/data` on Windows.
      4. `~/.config/commclient/data` on POSIX.
      5. `<PROJECT_ROOT>/data`  (dev fallback relative to this file).
    """
    for env_var in ("COMMCLIENT_DATA_DIR", "ELECTRON_DATA_DIR"):
        v = os.environ.get(env_var)
        if v:
            p = Path(v).expanduser()
            p.mkdir(parents=True, exist_ok=True)
            return p

    if sys.platform.startswith("win"):
        appdata = os.environ.get("APPDATA")
        if appdata:
            p = Path(appdata) / "CommClient" / "data"
            p.mkdir(parents=True, exist_ok=True)
            return p

    # POSIX fallback
    if not sys.platform.startswith("win"):
        xdg = os.environ.get("XDG_CONFIG_HOME")
        base = Path(xdg) if xdg else (Path.home() / ".config")
        p = base / "commclient" / "data"
        p.mkdir(parents=True, exist_ok=True)
        return p

    # Last-resort fallback: adjacent to the source tree. Safe for dev runs.
    project_root = Path(__file__).resolve().parent.parent.parent
    p = project_root / "data"
    p.mkdir(parents=True, exist_ok=True)
    return p


def secrets_file_path() -> Path:
    """Return the absolute path to the secrets JSON file."""
    return _resolve_data_dir() / _SECRETS_FILENAME


# ─────────────────────────────────────────────────────────────────────────────
# File I/O helpers
# ─────────────────────────────────────────────────────────────────────────────


def _read_existing(path: Path) -> dict[str, Any] | None:
    try:
        if not path.is_file():
            return None
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return None
        return data
    except (OSError, ValueError) as exc:
        print(
            f"[persistent_secrets] failed to read {path}: {exc}; "
            "regenerating a fresh file",
            file=sys.stderr,
        )
        return None


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    """Write the payload atomically (temp file + rename)."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except (OSError, AttributeError):
                pass
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _harden_permissions(path: Path) -> None:
    """Best-effort permission hardening. Failures are non-fatal."""
    try:
        if sys.platform.startswith("win"):
            # icacls: remove inheritance, grant only current user RW.
            import subprocess  # local import — only on Windows
            user = os.environ.get("USERNAME") or ""
            if user:
                try:
                    subprocess.run(
                        ["icacls", str(path), "/inheritance:r"],
                        check=False,
                        capture_output=True,
                        timeout=5,
                    )
                    subprocess.run(
                        ["icacls", str(path), "/grant:r", f"{user}:(R,W)"],
                        check=False,
                        capture_output=True,
                        timeout=5,
                    )
                except Exception:
                    pass
        else:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0600
    except OSError:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────


def ensure_persistent_secrets_loaded() -> dict[str, str]:
    """
    Load persisted secrets into `os.environ` (or create them on first run).

    Returns
    -------
    dict[str, str]
        The subset of secrets that were applied to the process environment.
        Keys: `JWT_SECRET`, `COMMCLIENT_E2EE_MASTER_SEED`,
        `MEDIASOUP_CONTROL_TOKEN`.

    Idempotent. Safe to call from multiple import paths.
    """
    global _already_loaded

    with _load_lock:
        if _already_loaded:
            return {
                k: os.environ[k]
                for k in (
                    "JWT_SECRET",
                    "COMMCLIENT_E2EE_MASTER_SEED",
                    "MEDIASOUP_CONTROL_TOKEN",
                )
                if k in os.environ
            }

        path = secrets_file_path()
        existing = _read_existing(path) or {}

        changed = False
        if existing.get("version") != _SECRETS_VERSION:
            existing["version"] = _SECRETS_VERSION
            changed = True

        if not isinstance(existing.get("jwt_secret"), str) or not existing["jwt_secret"]:
            existing["jwt_secret"] = secrets.token_urlsafe(_JWT_SECRET_BYTES)
            changed = True

        if not isinstance(existing.get("e2ee_master_seed"), str) or not existing["e2ee_master_seed"]:
            existing["e2ee_master_seed"] = secrets.token_urlsafe(_E2EE_SEED_BYTES)
            changed = True

        if not isinstance(existing.get("mediasoup_control_token"), str) or not existing["mediasoup_control_token"]:
            existing["mediasoup_control_token"] = secrets.token_urlsafe(_WORKER_TOKEN_BYTES)
            changed = True

        if changed:
            try:
                _atomic_write(path, existing)
                _harden_permissions(path)
            except OSError as exc:
                # Failure to persist is NOT fatal — fall back to in-memory
                # random secrets so the server can still boot. Log and move
                # on.
                print(
                    f"[persistent_secrets] failed to persist {path}: {exc}; "
                    "continuing with in-memory secrets",
                    file=sys.stderr,
                )

        # Apply to environ — but never override an explicit operator override.
        applied: dict[str, str] = {}

        def _apply(env_key: str, value: str) -> None:
            if os.environ.get(env_key):
                # Operator has set an explicit value; don't overwrite.
                return
            os.environ[env_key] = value
            applied[env_key] = value

        _apply("JWT_SECRET", existing["jwt_secret"])
        _apply("COMMCLIENT_E2EE_MASTER_SEED", existing["e2ee_master_seed"])
        _apply("MEDIASOUP_CONTROL_TOKEN", existing["mediasoup_control_token"])

        _already_loaded = True
        return applied


def rotate_jwt_secret() -> str:
    """
    Rotate the persisted JWT_SECRET. Called from admin tooling only.

    Returns the new secret. Every client will need to re-login afterwards.
    """
    global _already_loaded
    with _load_lock:
        path = secrets_file_path()
        data = _read_existing(path) or {"version": _SECRETS_VERSION}
        data["jwt_secret"] = secrets.token_urlsafe(_JWT_SECRET_BYTES)
        _atomic_write(path, data)
        _harden_permissions(path)
        os.environ["JWT_SECRET"] = data["jwt_secret"]
        _already_loaded = True
        return data["jwt_secret"]


__all__ = [
    "ensure_persistent_secrets_loaded",
    "rotate_jwt_secret",
    "secrets_file_path",
]
