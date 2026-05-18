"""Compression layer — zlib for relay payloads above a threshold.

Relay HTTP bodies between peers can carry large JSON envelopes
(message batches, gossip lists, file-offer manifests). Compressing
them above ``MIN_COMPRESS_BYTES`` typically cuts bandwidth 60-80%
for JSON-heavy payloads at < 1ms CPU cost per request.

Two pure functions:

  * ``maybe_compress(data) → (compressed_bytes, was_compressed)``
  * ``maybe_decompress(data, was_compressed) → bytes``

Plus counters for the admin dashboard.

Caller adds an HTTP header (``X-Helen-Compressed: 1``) when the
payload was compressed; receiver checks the header before
decompressing. Header-less paths stay backward-compatible.
"""

from __future__ import annotations

import os
import threading
import zlib
from typing import Tuple


def _i(env: str, default: int) -> int:
    try:
        return int(os.environ.get(env) or default)
    except (TypeError, ValueError):
        return default


MIN_COMPRESS_BYTES = _i("HELEN_COMPRESS_MIN_BYTES", 1024)
COMPRESS_LEVEL     = _i("HELEN_COMPRESS_LEVEL", 1)  # fastest, still ~70% on JSON


HEADER_NAME  = "X-Helen-Compressed"
HEADER_VALUE = "zlib"


class _Stats:
    _lock = threading.Lock()
    compressed_count = 0
    skipped_count = 0
    total_in_bytes = 0
    total_out_bytes = 0
    decompressed_count = 0
    decompress_failures = 0

    @classmethod
    def snapshot(cls) -> dict:
        with cls._lock:
            ratio = (
                round(100.0 * cls.total_out_bytes / cls.total_in_bytes, 2)
                if cls.total_in_bytes > 0 else None
            )
            return {
                "min_bytes":           MIN_COMPRESS_BYTES,
                "level":               COMPRESS_LEVEL,
                "compressed_count":    cls.compressed_count,
                "skipped_count":       cls.skipped_count,
                "total_in_bytes":      cls.total_in_bytes,
                "total_out_bytes":     cls.total_out_bytes,
                "compression_ratio_pct": ratio,
                "decompressed_count":  cls.decompressed_count,
                "decompress_failures": cls.decompress_failures,
            }

    @classmethod
    def record_compress(cls, in_n: int, out_n: int, did: bool) -> None:
        with cls._lock:
            if did:
                cls.compressed_count += 1
                cls.total_in_bytes += in_n
                cls.total_out_bytes += out_n
            else:
                cls.skipped_count += 1


def maybe_compress(data: bytes) -> Tuple[bytes, bool]:
    """Compress ``data`` when it exceeds the threshold and the
    compressed result is actually smaller. Returns ``(out, did)``.
    """
    if not data or len(data) < MIN_COMPRESS_BYTES:
        _Stats.record_compress(len(data), len(data), False)
        return data, False
    try:
        out = zlib.compress(data, level=COMPRESS_LEVEL)
    except Exception:
        _Stats.record_compress(len(data), len(data), False)
        return data, False
    if len(out) >= len(data):
        # Compression made it bigger (random / already-encoded data).
        _Stats.record_compress(len(data), len(data), False)
        return data, False
    _Stats.record_compress(len(data), len(out), True)
    return out, True


def maybe_decompress(data: bytes, *, was_compressed: bool) -> bytes:
    """Inverse — used by receivers that saw the X-Helen-Compressed
    header. Returns the original payload."""
    if not was_compressed:
        return data
    try:
        out = zlib.decompress(data)
        with _Stats._lock:
            _Stats.decompressed_count += 1
        return out
    except Exception:
        with _Stats._lock:
            _Stats.decompress_failures += 1
        # Fall back to the wrapped value — better than 500 the request.
        return data


def header_pair(was_compressed: bool) -> dict[str, str]:
    """Return the header dict for outbound requests."""
    if was_compressed:
        return {HEADER_NAME: HEADER_VALUE}
    return {}


def is_compressed_header(headers: dict | None) -> bool:
    if not headers:
        return False
    # Case-insensitive lookup since httpx normalises and aiohttp doesn't.
    for k, v in headers.items():
        if k.lower() == HEADER_NAME.lower() and str(v).lower() == HEADER_VALUE:
            return True
    return False


def status() -> dict:
    return _Stats.snapshot()
