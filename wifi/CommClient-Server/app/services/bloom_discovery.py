"""
Bloom filter discovery — compact peer membership advertisement.

Gossip currently sends the full ``known_peers`` list (up to 500
entries × ~64 bytes each = ~32 KB per peer per cycle). At cluster
sizes above a few thousand, that bandwidth becomes the dominant
cost of running the mesh.

A Bloom filter compresses a set of N items into M bits with a
controllable false-positive rate. For Helen we use:

  * M = 16,384 bits (2 KiB)
  * K = 7 hash functions
  * N up to 5,000 server_ids → false positive rate ≈ 0.8%

A peer publishes its Bloom of "server_ids I've heard of" each
gossip cycle. A receiving peer can:

  * Cheaply test "does this peer know about server X?" without
    actually transferring X's record.
  * Send only the records that the *peer* doesn't have, instead of
    the full 500-entry blob. This collapses per-cycle bandwidth
    for converged clusters down to near-zero.

False positives waste bandwidth (we skip pushing a record the peer
actually didn't know about) but are *never* harmful — gossip is
self-correcting; the peer will learn it next cycle.

Algorithm (BLAKE2b multi-hash):
  Use one BLAKE2b digest, slice it into K integers each modulo M.
  This is faster than running K separate hashes and gives the same
  asymptotic FPR for our parameters.
"""

from __future__ import annotations

import hashlib
import math
import threading
from typing import Iterable


DEFAULT_M_BITS = 16_384      # 2 KiB on the wire
DEFAULT_K_HASH = 7


def _hash_indices(item: str, k: int, m: int) -> list[int]:
    """Slice one BLAKE2b digest into K position indices in [0, m)."""
    # 8 bytes per index × K up to 8 → 64 bytes is plenty (BLAKE2b max=64).
    digest = hashlib.blake2b(item.encode(), digest_size=min(64, 8 * k)).digest()
    out = []
    for i in range(k):
        chunk = digest[i * 8: (i + 1) * 8]
        v = int.from_bytes(chunk, "big") % m
        out.append(v)
    return out


class BloomFilter:
    """Plain Bloom filter — add and test only, no remove (would
    require a counting Bloom; not needed here since each cycle
    produces a fresh filter)."""

    def __init__(self, m_bits: int = DEFAULT_M_BITS, k_hash: int = DEFAULT_K_HASH):
        self._lock = threading.RLock()
        self.m = int(m_bits)
        self.k = int(k_hash)
        # Use a bytearray for compact storage.
        self._bits = bytearray((self.m + 7) // 8)
        self._added = 0

    def add(self, item: str) -> None:
        with self._lock:
            for idx in _hash_indices(item, self.k, self.m):
                self._bits[idx >> 3] |= (1 << (idx & 7))
            self._added += 1

    def add_all(self, items: Iterable[str]) -> None:
        for it in items:
            self.add(it)

    def __contains__(self, item: str) -> bool:
        with self._lock:
            for idx in _hash_indices(item, self.k, self.m):
                if not (self._bits[idx >> 3] & (1 << (idx & 7))):
                    return False
            return True

    def to_bytes(self) -> bytes:
        with self._lock:
            return bytes(self._bits)

    @classmethod
    def from_bytes(cls, data: bytes,
                   m_bits: int = DEFAULT_M_BITS,
                   k_hash: int = DEFAULT_K_HASH) -> "BloomFilter":
        bf = cls(m_bits=m_bits, k_hash=k_hash)
        bf._bits = bytearray(data[:(m_bits + 7) // 8])
        return bf

    def estimated_fpr(self) -> float:
        """(1 - e^(-kn/m))^k — analytic FPR for current load."""
        if self._added == 0:
            return 0.0
        x = 1.0 - math.exp(-self.k * self._added / self.m)
        return round(x ** self.k, 6)

    def stats(self) -> dict:
        with self._lock:
            ones = sum(bin(b).count("1") for b in self._bits)
            return {
                "m_bits":   self.m,
                "k_hash":   self.k,
                "added":    self._added,
                "ones":     ones,
                "fill_pct": round(100.0 * ones / self.m, 2),
                "fpr":      self.estimated_fpr(),
            }


# ── Helper for gossip integration ───────────────────────────────


def build_local_peer_filter() -> BloomFilter:
    """Snapshot every server_id we currently know about (peers + self)
    and return a fresh BloomFilter for advertising in gossip payloads.
    """
    bf = BloomFilter()
    try:
        from app.services.node_registry import get_registry
        reg = get_registry()
        for n in reg.nodes(include_dead=False):
            bf.add(n.node_id)
    except Exception:
        pass
    return bf


def diff_against_filter(peer_filter_bytes: bytes,
                        local_known: Iterable[str]) -> list[str]:
    """Given a peer's Bloom and our known peer ids, return the ids
    we should push (those almost certainly absent from the peer)."""
    bf = BloomFilter.from_bytes(peer_filter_bytes)
    return [sid for sid in local_known if sid not in bf]
