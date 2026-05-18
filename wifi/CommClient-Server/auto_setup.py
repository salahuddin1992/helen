"""
auto_setup.py — One-shot environment bootstrapper for CommClient-Server.

Run this once after cloning to make sure the dev environment is in a usable
state regardless of which Python version is installed:

  python auto_setup.py

What it does (idempotent — safe to re-run):
  1. Verifies Python version is 3.8+ (warns on >= 3.13 due to wheel maturity).
  2. Upgrades pip, setuptools, wheel inside the current interpreter.
  3. Installs requirements.txt with --prefer-binary so wheels are picked.
  4. Creates the data/, files/, logs/, migrations/versions/ directories.
  5. Copies .env.example -> .env if .env is missing.
  6. Runs `alembic upgrade head` to bring the SQLite DB up to date.
  7. Picks a free TCP port in 3000-3010 and prints it for the launcher.

Exit codes:
  0  — success
  1  — non-recoverable error (printed to stderr)
"""

from __future__ import annotations

# IMPORTANT: import compatibility shims first
import compat  # noqa: F401

import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = sys.executable


def log(msg: str) -> None:
    print(f"[auto_setup] {msg}", flush=True)


def err(msg: str) -> None:
    print(f"[auto_setup] ERROR: {msg}", file=sys.stderr, flush=True)


# ─────────────────────────────────────────────────────────────
# 1. Python version check
# ─────────────────────────────────────────────────────────────
def check_python_version() -> None:
    major, minor = sys.version_info[:2]
    log(f"Python {major}.{minor}.{sys.version_info.micro} at {PY}")
    if (major, minor) < (3, 8):
        err("Python 3.8+ is required. Please install a newer version.")
        sys.exit(1)
    if (major, minor) >= (3, 14):
        log("WARNING: Python 3.14+ detected — some wheels may be missing.")


# ─────────────────────────────────────────────────────────────
# 2. pip upgrade + dependency install
# ─────────────────────────────────────────────────────────────
def pip(*args: str) -> int:
    cmd = [PY, "-m", "pip", *args]
    log(" ".join(cmd))
    return subprocess.call(cmd)


def upgrade_packaging_tools() -> None:
    code = pip("install", "--upgrade", "pip", "setuptools", "wheel")
    if code != 0:
        log("WARNING: failed to upgrade pip/setuptools/wheel — continuing.")


def install_requirements() -> None:
    req = ROOT / "requirements.txt"
    if not req.exists():
        err(f"requirements.txt not found at {req}")
        sys.exit(1)
    code = pip(
        "install",
        "--prefer-binary",
        "--disable-pip-version-check",
        "-r",
        str(req),
    )
    if code != 0:
        err("dependency install failed — see pip output above.")
        sys.exit(1)


# ─────────────────────────────────────────────────────────────
# 3. Directory + .env scaffolding
# ─────────────────────────────────────────────────────────────
def ensure_dirs() -> None:
    for sub in ("data", "files", "logs", "migrations/versions"):
        d = ROOT / sub
        d.mkdir(parents=True, exist_ok=True)
        log(f"ensured dir: {d}")


def ensure_env_file() -> None:
    env = ROOT / ".env"
    example = ROOT / ".env.example"
    if env.exists():
        log(".env already exists — leaving untouched")
        return
    if example.exists():
        shutil.copyfile(example, env)
        log(f"created {env} from {example}")
    else:
        env.write_text(
            "# CommClient Server — auto-generated minimal .env\n"
            "HOST=0.0.0.0\nPORT=3000\nDEBUG=false\nLOG_LEVEL=INFO\n",
            encoding="utf-8",
        )
        log(f"created minimal {env} (no .env.example found)")


# ─────────────────────────────────────────────────────────────
# 4. Database migrations
# ─────────────────────────────────────────────────────────────
def run_migrations() -> None:
    try:
        code = subprocess.call(
            [PY, "-m", "alembic", "upgrade", "head"], cwd=str(ROOT)
        )
        if code != 0:
            log("WARNING: alembic upgrade returned non-zero exit code.")
    except FileNotFoundError:
        log("WARNING: alembic not on PATH — skipping migrations.")


# ─────────────────────────────────────────────────────────────
# 5. Free port detection (3000-3010)
# ─────────────────────────────────────────────────────────────
def find_free_port(start: int = 3000, end: int = 3010) -> int:
    for port in range(start, end + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("0.0.0.0", port))
                return port
            except OSError:
                continue
    return start  # fall back to the canonical port


def main() -> int:
    log(f"working directory: {ROOT}")
    check_python_version()
    upgrade_packaging_tools()
    install_requirements()
    ensure_dirs()
    ensure_env_file()
    run_migrations()
    port = find_free_port()
    log(f"selected free port: {port}")
    log("setup complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
