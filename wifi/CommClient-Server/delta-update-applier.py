"""
delta-update-applier.py — Phase 4 / Module S
============================================

Apply a binary delta produced by ``delta-update-builder.py``.

Workflow on the client side
---------------------------
1. Auto-updater fetches the patch directory (manifest.json + patch.bin).
2. Verifies ``old_sha256`` matches the installed binary.
3. Calls this script to produce the new binary in-place (atomic rename).
4. Verifies the resulting binary against ``new_sha256``.
5. Swaps in the new binary; restarts the service.

CLI:
    python delta-update-applier.py \
        --installed   "C:/Program Files/CommClient/Helen-Server.exe" \
        --patch-dir   "%TEMP%/helen-delta-1.2.0_to_1.3.0" \
        --output      "%TEMP%/Helen-Server.new.exe"
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

try:
    import bsdiff4
    _BSDIFF_AVAILABLE = True
except Exception:
    bsdiff4 = None  # type: ignore[assignment]
    _BSDIFF_AVAILABLE = False


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            buf = f.read(chunk)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


def _hdlt_apply(old: bytes, patch: bytes) -> bytes:
    """Apply HDLT fallback format (full replace)."""
    if not patch.startswith(b"HDLT"):
        raise ValueError("not a HDLT patch")
    size = int.from_bytes(patch[4:12], "big")
    return patch[12:12 + size]


def apply_patch(
    *,
    installed: Path,
    patch_dir: Path,
    output: Path,
) -> None:
    manifest_path = patch_dir / "manifest.json"
    patch_path = patch_dir / "patch.bin"

    if not manifest_path.is_file():
        raise FileNotFoundError(manifest_path)
    if not patch_path.is_file():
        raise FileNotFoundError(patch_path)

    manifest = json.loads(manifest_path.read_text())
    if manifest.get("schema") != "helen.delta.v1":
        raise ValueError(f"unsupported schema: {manifest.get('schema')!r}")

    expected_old = manifest["old_sha256"]
    expected_new = manifest["new_sha256"]
    algorithm = manifest["algorithm"]

    actual_old = sha256_file(installed)
    if actual_old != expected_old:
        raise ValueError(
            f"installed binary sha256 mismatch:\n"
            f"  expected: {expected_old}\n"
            f"  actual:   {actual_old}"
        )

    old_bytes = installed.read_bytes()
    patch_bytes = patch_path.read_bytes()

    if algorithm == "bsdiff4":
        if not _BSDIFF_AVAILABLE:
            raise RuntimeError("patch was built with bsdiff4 but bsdiff4 not installed")
        new_bytes = bsdiff4.patch(old_bytes, patch_bytes)  # type: ignore[union-attr]
    elif algorithm == "hdlt-fallback":
        new_bytes = _hdlt_apply(old_bytes, patch_bytes)
    else:
        raise ValueError(f"unknown algorithm: {algorithm!r}")

    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_suffix(output.suffix + ".part")
    tmp.write_bytes(new_bytes)

    actual_new = sha256_file(tmp)
    if actual_new != expected_new:
        tmp.unlink(missing_ok=True)
        raise ValueError(
            f"patched binary sha256 mismatch:\n"
            f"  expected: {expected_new}\n"
            f"  actual:   {actual_new}"
        )

    # Atomic swap (best-effort on Windows)
    if output.exists():
        output.unlink()
    tmp.rename(output)

    print(f"[delta-apply] OK — {output}  ({len(new_bytes):,} bytes)")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Apply a binary delta patch.")
    p.add_argument("--installed", required=True)
    p.add_argument("--patch-dir", required=True)
    p.add_argument("--output", required=True)
    a = p.parse_args(argv)
    try:
        apply_patch(
            installed=Path(a.installed),
            patch_dir=Path(a.patch_dir),
            output=Path(a.output),
        )
    except Exception as exc:
        print(f"[delta-apply] ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
