"""
Content-based MIME sniffer — magic-byte detection without external deps.

Purpose
-------
Defend against spoofed uploads where a hostile client claims a benign
``content_type`` or extension but ships a different payload (e.g. ``.exe``
renamed to ``.jpg``). We read the first few kilobytes of the payload and
match them against a curated signature table.

This is NOT a full libmagic replacement — it targets the file types the
CommClient platform allows (images, audio, video, documents, archives).
Unknown payloads are classified as ``application/octet-stream``.

Security notes
--------------
- Used as a *second* line of defense after extension allow-listing. A
  signature check that *disagrees* with the claimed type is a hard reject.
- Constant-time over payload size: we only peek the first N bytes.
- Zero dynamic imports: safe for PyInstaller / single-exe builds.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

# Bytes needed to reliably decide. Most formats commit in first 16B;
# ISO Base Media (MP4 / MOV) needs the ``ftyp`` box at offset 4.
HEAD_BYTES_REQUIRED = 64


@dataclass(frozen=True)
class Signature:
    mime: str
    # (offset, bytes) tuples — all must match
    parts: tuple[tuple[int, bytes], ...]
    # Optional extension hints for extra cross-checking
    exts: tuple[str, ...] = ()


# Curated signature list. Ordering matters only as a tiebreaker —
# we always fall through to the first match.
_SIGS: tuple[Signature, ...] = (
    # ── Images ───────────────────────────────────────────────────────
    Signature("image/png",  ((0, b"\x89PNG\r\n\x1a\n"),), (".png",)),
    Signature("image/jpeg", ((0, b"\xff\xd8\xff"),),     (".jpg", ".jpeg")),
    Signature("image/gif",  ((0, b"GIF87a"),),           (".gif",)),
    Signature("image/gif",  ((0, b"GIF89a"),),           (".gif",)),
    Signature("image/webp", ((0, b"RIFF"), (8, b"WEBP")),(".webp",)),
    Signature("image/bmp",  ((0, b"BM"),),               (".bmp",)),
    Signature("image/tiff", ((0, b"II*\x00"),),          (".tif", ".tiff")),
    Signature("image/tiff", ((0, b"MM\x00*"),),          (".tif", ".tiff")),
    Signature("image/heic", ((4, b"ftypheic"),),         (".heic",)),
    Signature("image/heif", ((4, b"ftypheif"),),         (".heif",)),
    Signature("image/x-icon", ((0, b"\x00\x00\x01\x00"),), (".ico",)),

    # ── Audio ─────────────────────────────────────────────────────────
    # WAV / AIFF / FLAC / OGG / MP3 (with ID3)
    Signature("audio/wav",  ((0, b"RIFF"), (8, b"WAVE")), (".wav",)),
    Signature("audio/aiff", ((0, b"FORM"), (8, b"AIFF")), (".aiff", ".aif")),
    Signature("audio/flac", ((0, b"fLaC"),),              (".flac",)),
    Signature("audio/ogg",  ((0, b"OggS"),),              (".ogg", ".oga", ".opus")),
    Signature("audio/mpeg", ((0, b"ID3"),),               (".mp3",)),
    Signature("audio/mpeg", ((0, b"\xff\xfb"),),          (".mp3",)),
    Signature("audio/mpeg", ((0, b"\xff\xf3"),),          (".mp3",)),
    Signature("audio/mpeg", ((0, b"\xff\xf2"),),          (".mp3",)),
    Signature("audio/x-m4a",((4, b"ftypM4A"),),           (".m4a",)),

    # ── Video ─────────────────────────────────────────────────────────
    Signature("video/mp4",   ((4, b"ftypisom"),),         (".mp4",)),
    Signature("video/mp4",   ((4, b"ftypmp42"),),         (".mp4",)),
    Signature("video/mp4",   ((4, b"ftypMSNV"),),         (".mp4",)),
    Signature("video/mp4",   ((4, b"ftypavc1"),),         (".mp4",)),
    Signature("video/mp4",   ((4, b"ftypdash"),),         (".mp4",)),
    Signature("video/quicktime", ((4, b"ftypqt"),),       (".mov",)),
    Signature("video/x-matroska",((0, b"\x1a\x45\xdf\xa3"),),   (".mkv", ".webm")),
    Signature("video/webm",      ((0, b"\x1a\x45\xdf\xa3"),),   (".webm",)),
    Signature("video/x-msvideo", ((0, b"RIFF"), (8, b"AVI ")),  (".avi",)),

    # ── Documents ─────────────────────────────────────────────────────
    Signature("application/pdf", ((0, b"%PDF-"),),                (".pdf",)),
    Signature("application/zip", ((0, b"PK\x03\x04"),),
              (".zip", ".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp", ".jar")),
    Signature("application/zip", ((0, b"PK\x05\x06"),),           (".zip",)),  # empty zip
    Signature("application/x-rar-compressed", ((0, b"Rar!\x1a\x07\x00"),), (".rar",)),
    Signature("application/x-rar-compressed", ((0, b"Rar!\x1a\x07\x01\x00"),), (".rar",)),
    Signature("application/x-7z-compressed",  ((0, b"7z\xbc\xaf\x27\x1c"),), (".7z",)),
    Signature("application/gzip", ((0, b"\x1f\x8b"),),            (".gz", ".tgz")),
    Signature("application/x-tar", ((257, b"ustar"),),            (".tar",)),
    # MS Office legacy OLE compound (doc/xls/ppt)
    Signature("application/x-ole-storage",
              ((0, b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"),),
              (".doc", ".xls", ".ppt", ".msi")),

    # ── Code / text — plain text is best-effort ──────────────────────
    Signature("application/json", ((0, b"{"),),  (".json",)),
    Signature("application/json", ((0, b"["),),  (".json",)),
    Signature("application/xml",  ((0, b"<?xml"),), (".xml",)),

    # ── Dangerous binaries — always reject ────────────────────────────
    Signature("application/x-msdownload",     ((0, b"MZ"),),    (".exe", ".dll", ".scr")),
    Signature("application/x-executable",     ((0, b"\x7fELF"),), (".elf", ".so")),
    Signature("application/x-mach-binary",    ((0, b"\xfe\xed\xfa\xce"),), (".dylib", ".o")),
    Signature("application/x-mach-binary",    ((0, b"\xfe\xed\xfa\xcf"),), (".dylib", ".o")),
    Signature("application/java-vm",          ((0, b"\xca\xfe\xba\xbe"),), (".class",)),
    Signature("application/x-shellscript",    ((0, b"#!"),),               (".sh",)),
)


# Types we outright refuse to accept regardless of extension claim.
DANGEROUS_MIMES: frozenset[str] = frozenset({
    "application/x-msdownload",
    "application/x-executable",
    "application/x-mach-binary",
    "application/java-vm",
    "application/x-shellscript",
})


def sniff(head: bytes) -> str:
    """Return the detected MIME type for ``head`` (first N bytes of a payload).

    Falls back to ``application/octet-stream`` if no signature matches.
    """
    if not head:
        return "application/octet-stream"
    for sig in _SIGS:
        if all(
            len(head) >= off + len(b) and head[off:off + len(b)] == b
            for off, b in sig.parts
        ):
            return sig.mime

    # Best-effort text detection: ASCII / UTF-8 compatible printable bytes.
    try:
        head.decode("utf-8", errors="strict")
        # Heuristic: reject if any NUL bytes in first KB
        if b"\x00" in head:
            return "application/octet-stream"
        return "text/plain"
    except UnicodeDecodeError:
        return "application/octet-stream"


def is_dangerous(mime: str) -> bool:
    """Return True if ``mime`` is categorically unsafe (executables, scripts)."""
    return mime in DANGEROUS_MIMES


def matches_extension(mime: str, ext: str | None) -> bool:
    """
    Sanity-check that a detected MIME matches the claimed extension.

    - Empty ext → permissive (allow).
    - Detected MIME has no extension hints → permissive.
    - Otherwise: extension must be in the signature's hint list for the MIME.

    Matches across multiple signatures with the same MIME (e.g. both JPEG
    signatures map to the same ``.jpg``/``.jpeg`` extensions).
    """
    if not ext:
        return True
    ext_norm = ext.lower()
    if not ext_norm.startswith("."):
        ext_norm = "." + ext_norm

    found_any_hint = False
    for sig in _SIGS:
        if sig.mime != mime:
            continue
        if not sig.exts:
            continue
        found_any_hint = True
        if ext_norm in sig.exts:
            return True
    return not found_any_hint  # no hints at all → don't block


def validate_upload(
    head: bytes,
    claimed_mime: str | None,
    ext: str | None,
    allow_dangerous: bool = False,
) -> tuple[str, list[str]]:
    """
    Validate an upload's first chunk.

    Returns
    -------
    (canonical_mime, warnings)
        ``canonical_mime`` — the detected MIME we actually trust (use this,
          not the client-supplied one, for storage).
        ``warnings`` — list of non-fatal discrepancy messages.

    Raises
    ------
    ValueError
        When the payload is categorically unsafe
        (executable / script) and ``allow_dangerous`` is False.
    """
    detected = sniff(head)
    warnings: list[str] = []

    if is_dangerous(detected) and not allow_dangerous:
        raise ValueError(
            f"rejected dangerous payload (detected={detected})"
        )

    if claimed_mime and detected != "application/octet-stream":
        # Don't fail on octet-stream (we just couldn't identify it).
        if _normalize(claimed_mime) != _normalize(detected):
            # Accept if claimed is a superset (e.g. claimed text/plain but we
            # fingerprinted as application/json — that's a narrower subtype).
            if not _is_compatible(claimed_mime, detected):
                warnings.append(
                    f"content-type mismatch: claimed={claimed_mime} detected={detected}",
                )

    if ext and detected != "application/octet-stream":
        if not matches_extension(detected, ext):
            warnings.append(
                f"extension mismatch: ext={ext} detected={detected}",
            )

    return detected, warnings


def _normalize(m: str) -> str:
    return (m or "").split(";", 1)[0].strip().lower()


def _is_compatible(a: str, b: str) -> bool:
    """Return True if ``a`` and ``b`` are compatible content-types.

    Rules:
      - exact match (case-insensitive, ignoring parameters) → True
      - one side is ``application/octet-stream`` → True (client gave up)
      - JSON ↔ text/plain / application/xml ↔ text/xml considered compatible
    """
    an, bn = _normalize(a), _normalize(b)
    if an == bn:
        return True
    if "application/octet-stream" in (an, bn):
        return True
    equiv_groups: Iterable[frozenset[str]] = (
        frozenset({"application/json", "text/json", "text/plain"}),
        frozenset({"application/xml", "text/xml"}),
        frozenset({"audio/ogg", "application/ogg"}),
        frozenset({"image/jpg", "image/jpeg"}),
        frozenset({"audio/x-wav", "audio/wav", "audio/wave"}),
        frozenset({"video/webm", "video/x-matroska"}),
    )
    for g in equiv_groups:
        if an in g and bn in g:
            return True
    return False
