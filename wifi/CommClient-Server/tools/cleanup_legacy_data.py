"""
tools/cleanup_legacy_data.py — Phase 4 / Module T
=================================================

Operator CLI that archives legacy ``data_*`` folders left behind by old
test runs (mesh100, mesh1k, e2e, smoke, twoinstance, xserver_dm, ...).

Safety
------
- **Never deletes.** Folders are *moved* into
  ``archive_legacy_data/<ISO8601>/<original_name>/``. Operators can
  restore them with ``--restore <archive_id>``.
- **Dry-run by default.** Pass ``--apply --confirm`` to actually move.
- **Idempotent.** Re-running skips folders that were already archived.

Usage
-----
::

    python -m tools.cleanup_legacy_data --scan
    python -m tools.cleanup_legacy_data --apply --confirm
    python -m tools.cleanup_legacy_data --list-archives
    python -m tools.cleanup_legacy_data --restore 2026-05-11T10-04-22Z

Requires Python 3.10+ (uses structural pattern matching).
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import shutil
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

try:
    import structlog
    log = structlog.get_logger("cleanup_legacy_data")
    _USE_STRUCTLOG = True
except Exception:
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    log = logging.getLogger("cleanup_legacy_data")  # type: ignore[assignment]
    _USE_STRUCTLOG = False


# ── constants ───────────────────────────────────────────────────────

# Glob patterns we consider "legacy". Tested empirically on the
# ``C:\Users\youse\c\wifi`` workspace.
LEGACY_PATTERNS: tuple[str, ...] = (
    "data_*",
    "data-*",
    "*_legacy",
    "data_smoke_*",
    "data_mesh*",
    "data_peer*",
    "data_xserver*",
    "data_twoinstance*",
    "data_3server*",
    "data_chain*",
    "tests_data_*",
)

# Folders we never touch even if their name matches a legacy pattern.
NEVER_TOUCH: frozenset[str] = frozenset({
    "data",                  # current production data dir
    "data_prod",
    "archive_legacy_data",   # our own archive root
})

ARCHIVE_ROOT_NAME = "archive_legacy_data"
INDEX_JSON = "_index.json"
INDEX_MD = "_index.md"


# ── data classes ────────────────────────────────────────────────────

@dataclass
class FolderReport:
    name: str
    path: str
    size_bytes: int
    file_count: int
    last_modified: str
    top_files: list[tuple[str, int]] = field(default_factory=list)

    def size_mb(self) -> float:
        return round(self.size_bytes / (1024 * 1024), 2)


@dataclass
class ArchiveEntry:
    archive_id: str           # ISO8601 timestamp (filesystem-safe)
    original_name: str
    original_path: str
    archived_path: str
    size_bytes: int
    file_count: int
    sha256_summary: str       # hash over (name + size + mtime) of all files

    def size_mb(self) -> float:
        return round(self.size_bytes / (1024 * 1024), 2)


# ── helpers ─────────────────────────────────────────────────────────

def _iter_files(root: Path) -> Iterable[Path]:
    for p in root.rglob("*"):
        if p.is_file():
            yield p


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


def _matches_legacy(name: str) -> bool:
    if name in NEVER_TOUCH:
        return False
    return any(fnmatch.fnmatch(name, pat) for pat in LEGACY_PATTERNS)


def _analyze_folder(folder: Path) -> FolderReport:
    total = 0
    count = 0
    biggest: list[tuple[str, int]] = []
    last_mtime: float = 0.0
    for p in _iter_files(folder):
        try:
            st = p.stat()
        except OSError:
            continue
        total += st.st_size
        count += 1
        if st.st_mtime > last_mtime:
            last_mtime = st.st_mtime
        biggest.append((str(p.relative_to(folder)), st.st_size))
    biggest.sort(key=lambda x: x[1], reverse=True)
    return FolderReport(
        name=folder.name,
        path=str(folder),
        size_bytes=total,
        file_count=count,
        last_modified=(
            datetime.fromtimestamp(last_mtime, timezone.utc).isoformat()
            if last_mtime else "unknown"
        ),
        top_files=biggest[:5],
    )


def _hash_summary(folder: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(_iter_files(folder)):
        try:
            st = p.stat()
        except OSError:
            continue
        h.update(p.name.encode("utf-8", errors="replace"))
        h.update(str(st.st_size).encode())
        h.update(str(int(st.st_mtime)).encode())
    return h.hexdigest()


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024  # type: ignore[assignment]
    return f"{n:.1f} PB"


# ── core ops ────────────────────────────────────────────────────────

def find_legacy(scan_root: Path) -> list[Path]:
    """Find every top-level folder under ``scan_root`` that matches a
    legacy pattern. Sorted by name for deterministic output."""
    hits: list[Path] = []
    if not scan_root.is_dir():
        return hits
    for child in sorted(scan_root.iterdir()):
        if not child.is_dir():
            continue
        if _matches_legacy(child.name):
            hits.append(child)
    return hits


def scan(scan_root: Path) -> list[FolderReport]:
    return [_analyze_folder(p) for p in find_legacy(scan_root)]


def apply(scan_root: Path, archive_root: Path) -> list[ArchiveEntry]:
    """Move every legacy folder under ``scan_root`` to a fresh archive
    bucket under ``archive_root``. Returns the entries that were moved."""
    archive_id = _now_iso()
    bucket = archive_root / archive_id
    bucket.mkdir(parents=True, exist_ok=True)

    entries: list[ArchiveEntry] = []
    for folder in find_legacy(scan_root):
        # Skip if folder was already moved by a previous run (shouldn't
        # happen because we matched it under scan_root, but defensive).
        dest = bucket / folder.name
        if dest.exists():
            log.info("skip_existing", folder=str(folder), archive=str(dest))
            continue

        rep = _analyze_folder(folder)
        sha = _hash_summary(folder)

        log.info(
            "archiving",
            folder=str(folder),
            size_mb=rep.size_mb(),
            files=rep.file_count,
            to=str(dest),
        )
        shutil.move(str(folder), str(dest))

        entries.append(ArchiveEntry(
            archive_id=archive_id,
            original_name=rep.name,
            original_path=rep.path,
            archived_path=str(dest),
            size_bytes=rep.size_bytes,
            file_count=rep.file_count,
            sha256_summary=sha,
        ))

    _write_indexes(archive_root, entries)
    return entries


def _write_indexes(archive_root: Path, new_entries: list[ArchiveEntry]) -> None:
    if not new_entries:
        return
    idx_json = archive_root / INDEX_JSON
    idx_md = archive_root / INDEX_MD

    existing: list[dict] = []
    if idx_json.exists():
        try:
            existing = json.loads(idx_json.read_text())
        except Exception:
            existing = []
    existing.extend(asdict(e) for e in new_entries)
    archive_root.mkdir(parents=True, exist_ok=True)
    idx_json.write_text(json.dumps(existing, indent=2))

    # Markdown report
    lines = [
        "# Legacy-Data Archive Index",
        "",
        f"_Last update: {datetime.now(timezone.utc).isoformat()}_",
        "",
        "| Archive ID | Original Name | Size | Files | SHA-256 (first 16) | Path |",
        "|------------|---------------|------|-------|--------------------|------|",
    ]
    for row in existing:
        lines.append(
            f"| `{row['archive_id']}` "
            f"| `{row['original_name']}` "
            f"| {_human(row['size_bytes'])} "
            f"| {row['file_count']} "
            f"| `{row['sha256_summary'][:16]}` "
            f"| `{row['archived_path']}` |"
        )
    idx_md.write_text("\n".join(lines) + "\n")


def list_archives(archive_root: Path) -> list[dict]:
    idx = archive_root / INDEX_JSON
    if not idx.exists():
        return []
    try:
        return json.loads(idx.read_text())
    except Exception:
        return []


def restore(archive_root: Path, archive_id: str, scan_root: Path) -> int:
    """Move folders from a given archive bucket back to ``scan_root``.
    Returns the count restored."""
    bucket = archive_root / archive_id
    if not bucket.is_dir():
        raise FileNotFoundError(f"archive bucket not found: {bucket}")
    count = 0
    for child in bucket.iterdir():
        if not child.is_dir():
            continue
        target = scan_root / child.name
        if target.exists():
            log.warning("restore_collision", folder=child.name)
            continue
        shutil.move(str(child), str(target))
        log.info("restored", folder=child.name, to=str(target))
        count += 1
    # Remove empty bucket
    try:
        bucket.rmdir()
    except OSError:
        pass
    return count


# ── CLI ─────────────────────────────────────────────────────────────

def _print_scan(reports: list[FolderReport]) -> None:
    if not reports:
        print("No legacy data folders detected.")
        return
    total = sum(r.size_bytes for r in reports)
    files = sum(r.file_count for r in reports)
    print(f"\nFound {len(reports)} legacy folder(s) — {_human(total)} in {files} file(s):\n")
    print(f"  {'Name':<32} {'Size':>10} {'Files':>8} {'Last modified':>22}")
    print(f"  {'-' * 32} {'-' * 10} {'-' * 8} {'-' * 22}")
    for r in reports:
        print(f"  {r.name:<32} {_human(r.size_bytes):>10} {r.file_count:>8} {r.last_modified:>22}")
        for fname, fsize in r.top_files:
            print(f"      {fname[:48]:<48} {_human(fsize):>10}")
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Archive legacy CommClient-Server data folders.",
    )
    parser.add_argument(
        "--scan-root",
        type=Path,
        default=Path(__file__).resolve().parent.parent.parent,
        help="Directory to scan (default: project parent).",
    )
    parser.add_argument(
        "--archive-root",
        type=Path,
        default=None,
        help="Archive destination (default: <scan-root>/archive_legacy_data).",
    )
    grp = parser.add_mutually_exclusive_group(required=True)
    grp.add_argument("--scan",           action="store_true")
    grp.add_argument("--apply",          action="store_true")
    grp.add_argument("--list-archives",  action="store_true")
    grp.add_argument("--restore",        metavar="ARCHIVE_ID")
    parser.add_argument("--confirm", action="store_true",
                        help="Required with --apply to actually move.")
    args = parser.parse_args(argv)

    scan_root: Path = args.scan_root.resolve()
    archive_root: Path = (args.archive_root or scan_root / ARCHIVE_ROOT_NAME).resolve()
    log.info("startup", scan_root=str(scan_root), archive_root=str(archive_root))

    match args:
        case _ if args.scan:
            reports = scan(scan_root)
            _print_scan(reports)
            return 0
        case _ if args.apply:
            if not args.confirm:
                print("ERROR: --apply requires --confirm to acknowledge it will MOVE files.",
                      file=sys.stderr)
                print("       (Dry-run preview:)\n", file=sys.stderr)
                _print_scan(scan(scan_root))
                return 1
            entries = apply(scan_root, archive_root)
            print(f"\nArchived {len(entries)} folder(s) → {archive_root}")
            for e in entries:
                print(f"  {e.original_name:<32}  {_human(e.size_bytes):>10}  →  {e.archived_path}")
            return 0
        case _ if args.list_archives:
            rows = list_archives(archive_root)
            if not rows:
                print("No archives recorded.")
                return 0
            print(f"\n{len(rows)} archive entries:\n")
            for r in rows:
                print(f"  [{r['archive_id']}] {r['original_name']:<32} "
                      f"{_human(r['size_bytes']):>10} "
                      f"sha256={r['sha256_summary'][:16]}")
            return 0
        case _ if args.restore:
            try:
                n = restore(archive_root, args.restore, scan_root)
            except FileNotFoundError as exc:
                print(f"ERROR: {exc}", file=sys.stderr)
                return 1
            print(f"Restored {n} folder(s) from archive {args.restore}.")
            return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
