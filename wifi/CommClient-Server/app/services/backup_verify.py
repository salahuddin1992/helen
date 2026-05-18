"""Backup verification — sha256 hashes + dry-run restore.

Walks the backup directory, computes a sha256 over each file, and
records the digests in a manifest. A dry-run restore opens each
backup file as SQLite and runs ``PRAGMA integrity_check`` without
overwriting the live DB.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path

from app.core.logging import get_logger

logger = get_logger(__name__)


def _backup_dir() -> Path:
    return Path(os.environ.get("COMMCLIENT_DATA_DIR",
                str(Path(__file__).resolve().parents[2] / "data"))) / "backups"


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def list_backups() -> list[dict]:
    out: list[dict] = []
    bd = _backup_dir()
    if not bd.is_dir():
        return out
    for p in sorted(bd.iterdir()):
        if not p.is_file():
            continue
        try:
            stat = p.stat()
            out.append({
                "name":       p.name,
                "path":       str(p),
                "size_bytes": stat.st_size,
                "mtime":      stat.st_mtime,
            })
        except Exception:
            continue
    return out


def verify_one(backup_path: str) -> dict:
    """Compute sha256 + dry-run integrity check on one backup file."""
    p = Path(backup_path)
    if not p.is_file():
        return {"ok": False, "error": "not_found", "path": backup_path}
    out: dict = {"path": str(p), "size_bytes": p.stat().st_size}
    try:
        out["sha256"] = _sha256_of(p)
    except Exception as e:
        out["sha256_error"] = str(e)[:120]
    # SQLite integrity check (read-only).
    try:
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        try:
            row = conn.execute("PRAGMA integrity_check;").fetchone()
            out["integrity"] = row[0] if row else "no_result"
            out["integrity_ok"] = (out["integrity"] == "ok")
        finally:
            conn.close()
    except Exception as e:
        out["integrity"] = f"error:{e}"
        out["integrity_ok"] = False
    out["ok"] = bool(out.get("integrity_ok") and out.get("sha256"))
    return out


def verify_all() -> dict:
    backups = list_backups()
    results = [verify_one(b["path"]) for b in backups]
    ok_count = sum(1 for r in results if r.get("ok"))
    return {
        "total":    len(results),
        "ok":       ok_count,
        "failed":   len(results) - ok_count,
        "backups":  results,
        "dir":      str(_backup_dir()),
    }
