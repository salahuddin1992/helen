"""
app.api.routes._phase4_meta — Phase 4 manifest module.

Not a router. Exposes a metadata dict + a single function the admin UI
can query to render a "Phase 4 status" card. Importing this module has
no side effects.

Usage from the admin UI / health endpoint::

    from app.api.routes._phase4_meta import PHASE4_INFO, get_optimization_status
    return {
        "phase4": PHASE4_INFO,
        "optimization": get_optimization_status(),
    }
"""

from __future__ import annotations

import os
import platform
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ── Static manifest ─────────────────────────────────────────────────

PHASE4_INFO: dict[str, Any] = {
    "phase":          4,
    "name":           "Performance & Hardening",
    "version":        "4.0.0",
    "released":       "2026-05-11",
    "modules": [
        {
            "id":          "R",
            "title":       "Server Domain Decomposition",
            "kind":        "facade",
            "package":     "app.domains",
            "files_added": 16,
        },
        {
            "id":          "S",
            "title":       "PyInstaller & Nuitka Optimization",
            "kind":        "build-pipeline",
            "files_added": 5,
            "artifacts":   ["dist-optimized/", "dist-nuitka/", "dist-deltas/"],
        },
        {
            "id":          "T",
            "title":       "Legacy Data Cleanup",
            "kind":        "operator-tool",
            "entrypoint":  "python -m tools.cleanup_legacy_data",
            "files_added": 2,
        },
        {
            "id":          "U",
            "title":       "WMIC Replacement (Win 11 23H2+)",
            "kind":        "client-runtime",
            "scope":       "CommClient-Desktop",
            "files_added": 2,
        },
        {
            "id":          "V",
            "title":       "Tests Coverage Pack",
            "kind":        "testing",
            "files_added": 27,
            "runtimes":    ["pytest", "vitest", "playwright-electron"],
        },
    ],
    "total_files_added": 52,
}


# ── Runtime probes ──────────────────────────────────────────────────

def _check_path(path: str) -> dict[str, Any]:
    p = Path(path)
    exists = p.exists()
    return {
        "path":   str(p),
        "exists": exists,
        "size_bytes": (sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
                       if exists and p.is_dir() else
                       (p.stat().st_size if exists else 0)),
    }


def get_optimization_status() -> dict[str, Any]:
    """Inspect the local filesystem for evidence that the optimized
    build pipeline has been exercised. Used by the admin UI to display
    'last build' info without requiring a separate datastore.

    Always returns a dict — never raises. Missing artifacts are reported
    as ``exists: false`` rather than absent keys.
    """
    project_root = Path(__file__).resolve().parents[3]
    artifacts = {
        "pyinstaller_baseline":  _check_path(str(project_root / "dist" / "Helen-Server")),
        "pyinstaller_optimized": _check_path(str(project_root / "dist-optimized" / "Helen-Server")),
        "nuitka":                _check_path(str(project_root / "dist-nuitka" / "Helen-Server")),
        "deltas":                _check_path(str(project_root / "dist-deltas")),
        "archive_legacy_data":   _check_path(str(project_root.parent / "archive_legacy_data")),
    }

    # External tooling availability
    tools = {
        "upx_in_path":      bool(shutil.which("upx")),
        "nuitka_installed": _module_installed("nuitka"),
        "bsdiff4_installed": _module_installed("bsdiff4"),
    }

    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "host": {
            "platform":      platform.platform(),
            "python":        sys.version.split()[0],
            "executable":    sys.executable,
            "pid":           os.getpid(),
        },
        "artifacts": artifacts,
        "tools":     tools,
        "domains_facade_loaded": _domains_loaded(),
    }


def _module_installed(name: str) -> bool:
    try:
        __import__(name)
        return True
    except Exception:
        return False


def _domains_loaded() -> dict[str, Any]:
    """Lightweight probe of app.domains availability."""
    try:
        from app.domains._registry import get_summary
        return get_summary()
    except Exception as exc:
        return {"available": False, "error": str(exc)}


__all__ = [
    "PHASE4_INFO",
    "get_optimization_status",
]
