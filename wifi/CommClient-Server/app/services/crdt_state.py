"""
Conflict-free Replicated Data Types — state that converges without
coordination.

When two peers concurrently update the same value during a network
partition, last-write-wins silently throws one update away. CRDTs
solve this by giving each value a merge function that's:

  * commutative   — order of merges doesn't matter
  * associative   — grouping of merges doesn't matter
  * idempotent    — merging the same state twice is a no-op

This module ships four building blocks that cover ~90% of the cases
the cluster actually needs:

  * **GCounter**         — grow-only counter (chat-room participant
                            tally, message count)
  * **PNCounter**         — increments + decrements (online users)
  * **ORSet**             — observed-remove set (room members,
                            blocklist union)
  * **LWWRegister**        — last-write-wins single value with vector
                            clock tiebreak (room name, capacity)

Storage is intentionally caller-controlled — the CRDTs are pure data
structures and serialize to/from JSON dicts so they can sit in any
DB column or message payload.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


# ── G-Counter (grow-only) ───────────────────────────────────────


@dataclass
class GCounter:
    """Grow-only counter: per-node bucket, value is the sum of buckets."""
    self_id: str
    buckets: dict[str, int] = field(default_factory=dict)

    def increment(self, n: int = 1) -> int:
        if n < 0:
            raise ValueError("GCounter only accepts positive increments")
        self.buckets[self.self_id] = self.buckets.get(self.self_id, 0) + n
        return self.value()

    def value(self) -> int:
        return sum(self.buckets.values())

    def merge(self, other: "GCounter | dict") -> "GCounter":
        peer = other.buckets if isinstance(other, GCounter) else dict(other or {})
        for k, v in peer.items():
            self.buckets[k] = max(self.buckets.get(k, 0), int(v))
        return self

    def to_dict(self) -> dict:
        return {"buckets": dict(self.buckets)}

    @classmethod
    def from_dict(cls, data: dict, self_id: str) -> "GCounter":
        return cls(self_id=self_id, buckets=dict(data.get("buckets") or {}))


# ── PN-Counter (positive + negative) ────────────────────────────


@dataclass
class PNCounter:
    """Two G-Counters under the hood: positives and negatives.

    The visible value = sum(p) - sum(n). Decrement increments the
    negatives bucket; increment increments the positives bucket. Both
    grow monotonically so merge is just elementwise max on each side.
    """
    self_id: str
    positives: GCounter = field(default=None)  # type: ignore
    negatives: GCounter = field(default=None)  # type: ignore

    def __post_init__(self) -> None:
        if self.positives is None:
            self.positives = GCounter(self_id=self.self_id)
        if self.negatives is None:
            self.negatives = GCounter(self_id=self.self_id)

    def increment(self, n: int = 1) -> int:
        self.positives.increment(n)
        return self.value()

    def decrement(self, n: int = 1) -> int:
        self.negatives.increment(n)
        return self.value()

    def value(self) -> int:
        return self.positives.value() - self.negatives.value()

    def merge(self, other: "PNCounter | dict") -> "PNCounter":
        if isinstance(other, PNCounter):
            self.positives.merge(other.positives)
            self.negatives.merge(other.negatives)
        else:
            d = dict(other or {})
            self.positives.merge(d.get("positives") or {})
            self.negatives.merge(d.get("negatives") or {})
        return self

    def to_dict(self) -> dict:
        return {
            "positives": self.positives.to_dict(),
            "negatives": self.negatives.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict, self_id: str) -> "PNCounter":
        return cls(
            self_id=self_id,
            positives=GCounter.from_dict(data.get("positives") or {}, self_id),
            negatives=GCounter.from_dict(data.get("negatives") or {}, self_id),
        )


# ── OR-Set (observed-remove) ────────────────────────────────────


@dataclass
class ORSet:
    """Observed-remove set — add survives concurrent remove.

    Each element gets a unique tag on add (server_id × counter). To
    remove we move every observed tag to the tombstone set. On merge
    the visible set is (all_adds − all_removes) by tag, which gives
    the "add wins under concurrent remove" property without breaking
    associativity.
    """
    self_id: str
    adds:    dict[str, list[str]] = field(default_factory=dict)
    removes: dict[str, list[str]] = field(default_factory=dict)
    _seq:    int = 0

    def _new_tag(self) -> str:
        self._seq += 1
        return f"{self.self_id}:{self._seq}"

    def add(self, element: Any) -> None:
        key = str(element)
        self.adds.setdefault(key, []).append(self._new_tag())

    def remove(self, element: Any) -> None:
        key = str(element)
        observed = list(self.adds.get(key) or [])
        if not observed:
            return
        bucket = self.removes.setdefault(key, [])
        for tag in observed:
            if tag not in bucket:
                bucket.append(tag)

    def contains(self, element: Any) -> bool:
        key = str(element)
        adds = set(self.adds.get(key) or [])
        rems = set(self.removes.get(key) or [])
        return bool(adds - rems)

    def value(self) -> set:
        out = set()
        for key, tags in self.adds.items():
            rems = set(self.removes.get(key) or [])
            if set(tags) - rems:
                out.add(key)
        return out

    def merge(self, other: "ORSet | dict") -> "ORSet":
        if isinstance(other, ORSet):
            peer_adds, peer_rems = other.adds, other.removes
        else:
            d = dict(other or {})
            peer_adds = dict(d.get("adds") or {})
            peer_rems = dict(d.get("removes") or {})
        for key, tags in peer_adds.items():
            mine = self.adds.setdefault(key, [])
            for t in tags:
                if t not in mine:
                    mine.append(t)
        for key, tags in peer_rems.items():
            mine = self.removes.setdefault(key, [])
            for t in tags:
                if t not in mine:
                    mine.append(t)
        return self

    def to_dict(self) -> dict:
        return {
            "adds":    {k: list(v) for k, v in self.adds.items()},
            "removes": {k: list(v) for k, v in self.removes.items()},
            "_seq":    self._seq,
        }

    @classmethod
    def from_dict(cls, data: dict, self_id: str) -> "ORSet":
        return cls(
            self_id=self_id,
            adds=dict(data.get("adds") or {}),
            removes=dict(data.get("removes") or {}),
            _seq=int(data.get("_seq") or 0),
        )


# ── LWW Register (last-write-wins with vector-clock tiebreak) ───


@dataclass
class LWWRegister:
    """Single-value register. Writes carry a wallclock timestamp and
    the writer's node_id. On merge the larger timestamp wins; ties
    are broken by lexicographic ``node_id`` so two concurrent writes
    with the same wallclock converge to the same value across peers.
    """
    self_id: str
    value: Any = None
    timestamp: float = 0.0
    writer: str = ""

    def write(self, value: Any) -> None:
        self.value = value
        self.timestamp = time.time()
        self.writer = self.self_id

    def merge(self, other: "LWWRegister | dict") -> "LWWRegister":
        if isinstance(other, LWWRegister):
            o_ts, o_writer, o_value = other.timestamp, other.writer, other.value
        else:
            d = dict(other or {})
            o_ts = float(d.get("timestamp") or 0.0)
            o_writer = str(d.get("writer") or "")
            o_value = d.get("value")
        if o_ts > self.timestamp or (
            o_ts == self.timestamp and o_writer > self.writer
        ):
            self.value = o_value
            self.timestamp = o_ts
            self.writer = o_writer
        return self

    def to_dict(self) -> dict:
        return {
            "value":     self.value,
            "timestamp": self.timestamp,
            "writer":    self.writer,
        }

    @classmethod
    def from_dict(cls, data: dict, self_id: str) -> "LWWRegister":
        return cls(
            self_id=self_id,
            value=data.get("value"),
            timestamp=float(data.get("timestamp") or 0.0),
            writer=str(data.get("writer") or ""),
        )
