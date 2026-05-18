"""
delta-update-builder.py — Phase 4 / Module S
============================================

Generate a binary delta (patch) between two Helen-Server build artifacts.
Used by the auto-update channel to ship 2-5 MB patch downloads instead of
the full 95-185 MB binary on every release.

Algorithm
---------
* Primary: ``bsdiff4`` — Colin Percival's bsdiff, ported to pure-Python C
  extension. Produces compact patches (~5% of full binary for typical
  point releases).
* Fallback: ``difflib.diff_bytes`` line-based diff over base64-encoded
  binary. Slower + larger but does not require a C compiler.

Outputs
-------
A patch directory under ``dist-deltas/<from>_to_<to>/`` containing:

    patch.bin            (binary delta)
    manifest.json        (algorithm, from-version, to-version, sha256s)
    SHA256SUMS.txt       (manifest of every artifact)

Use ``delta-update-applier.py`` (companion) on the client side.

CLI
---

    python delta-update-builder.py \
        --old dist/Helen-Server/Helen-Server.exe \
        --new dist-optimized/Helen-Server/Helen-Server.exe \
        --from-version 1.2.0 \
        --to-version   1.3.0 \
        --output       dist-deltas

Exit codes: 0 success, 1 arg/IO error, 2 bsdiff missing without --force-fallback.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import bsdiff4
    _BSDIFF_AVAILABLE = True
except Exception:
    bsdiff4 = None  # type: ignore[assignment]
    _BSDIFF_AVAILABLE = False


# ── helpers ───────────────────────────────────────────────────────────

def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            buf = f.read(chunk)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


def fallback_diff(old: bytes, new: bytes) -> bytes:
    """Pure-Python fallback when bsdiff4 isn't installed.

    Uses a coarse block-replace format:
      4-byte big-endian header with magic ``HDLT`` (Helen DeLTa)
      followed by raw bytes of ``new`` (i.e. full-replace patch).

    NOT space-efficient — encourages installing bsdiff4 — but always works.
    """
    return b"HDLT" + len(new).to_bytes(8, "big") + new


def fallback_apply(old: bytes, patch: bytes) -> bytes:
    if not patch.startswith(b"HDLT"):
        raise ValueError("not a HDLT fallback patch")
    n = int.from_bytes(patch[4:12], "big")
    return patch[12:12 + n]


# ── main ──────────────────────────────────────────────────────────────

def build_patch(
    *,
    old_path: Path,
    new_path: Path,
    from_version: str,
    to_version: str,
    output_root: Path,
    force_fallback: bool = False,
) -> Path:
    if not old_path.is_file():
        raise FileNotFoundError(old_path)
    if not new_path.is_file():
        raise FileNotFoundError(new_path)

    old_sha = sha256_file(old_path)
    new_sha = sha256_file(new_path)

    if old_sha == new_sha:
        raise ValueError("old and new are identical — nothing to patch")

    use_bsdiff = _BSDIFF_AVAILABLE and not force_fallback
    algorithm = "bsdiff4" if use_bsdiff else "hdlt-fallback"

    print(f"[delta-build] algorithm = {algorithm}")
    print(f"[delta-build] old: {old_path.name}  sha256={old_sha[:16]}…")
    print(f"[delta-build] new: {new_path.name}  sha256={new_sha[:16]}…")

    old_bytes = old_path.read_bytes()
    new_bytes = new_path.read_bytes()

    if use_bsdiff:
        patch = bsdiff4.diff(old_bytes, new_bytes)  # type: ignore[union-attr]
    else:
        patch = fallback_diff(old_bytes, new_bytes)

    out_dir = output_root / f"{from_version}_to_{to_version}"
    out_dir.mkdir(parents=True, exist_ok=True)

    patch_path = out_dir / "patch.bin"
    patch_path.write_bytes(patch)

    manifest = {
        "schema":        "helen.delta.v1",
        "generated_at":  datetime.now(timezone.utc).isoformat(),
        "algorithm":     algorithm,
        "from_version":  from_version,
        "to_version":    to_version,
        "old_filename":  old_path.name,
        "new_filename":  new_path.name,
        "old_sha256":    old_sha,
        "new_sha256":    new_sha,
        "patch_sha256":  hashlib.sha256(patch).hexdigest(),
        "old_size":      len(old_bytes),
        "new_size":      len(new_bytes),
        "patch_size":    len(patch),
        "compression_ratio": round(len(patch) / max(len(new_bytes), 1), 4),
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

    # SHA256SUMS.txt
    lines = [
        f"{old_sha}  {old_path.name}",
        f"{new_sha}  {new_path.name}",
        f"{manifest['patch_sha256']}  patch.bin",
    ]
    (out_dir / "SHA256SUMS.txt").write_text("\n".join(lines) + "\n")

    print(f"[delta-build] patch written → {patch_path}")
    print(
        f"[delta-build] size: {len(patch):,} bytes "
        f"({manifest['compression_ratio']*100:.1f}% of new)"
    )
    return out_dir


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Build a binary delta patch.")
    p.add_argument("--old", required=True, help="Path to old binary.")
    p.add_argument("--new", required=True, help="Path to new binary.")
    p.add_argument("--from-version", required=True)
    p.add_argument("--to-version",   required=True)
    p.add_argument("--output", default="dist-deltas", help="Output root dir.")
    p.add_argument("--force-fallback", action="store_true",
                   help="Use HDLT fallback even if bsdiff4 is available.")
    args = p.parse_args(argv)

    if not _BSDIFF_AVAILABLE and not args.force_fallback:
        print(
            "[delta-build] bsdiff4 not installed; install with `pip install bsdiff4` "
            "or pass --force-fallback to use the (much larger) HDLT format.",
            file=sys.stderr,
        )
        return 2

    try:
        build_patch(
            old_path=Path(args.old),
            new_path=Path(args.new),
            from_version=args.from_version,
            to_version=args.to_version,
            output_root=Path(args.output),
            force_fallback=args.force_fallback,
        )
    except Exception as exc:
        print(f"[delta-build] ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
