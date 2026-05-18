"""
Vector clocks — causal ordering for distributed events.

A wallclock timestamp tells you *when* something was recorded but not
*what depended on what*. Two writes that happen one second apart on
different machines can have any causal relationship — independent,
or one caused the other and the network was slow. Last-write-wins
(used by reconciliation) silently drops the loser even if it carried
information that should have merged.

Vector clocks fix that: each event carries a per-node counter map.
Two clocks compare as one of:

  * **before**     — A's vector ≤ B's element-wise, with at least one strict <
  * **after**      — B is before A
  * **equal**      — same event
  * **concurrent** — neither is before the other (real conflict)

For events that are *concurrent*, the application layer (or a CRDT
merge function) decides what to do — there is no automatic winner.

Usage
-----
    vc = VectorClock(self_id="server-A")
    vc.tick()                       # local event
    payload = vc.to_dict()          # send over the wire
    other = VectorClock.from_dict(payload, self_id="server-A")
    vc.merge(other)                 # absorb peer's view

This module is deliberately storage-agnostic — vector clocks are
in-memory by design, persisted by the caller alongside the event
they tag (chat message, room state change, etc).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class CausalOrder(str, Enum):
    BEFORE     = "before"
    AFTER      = "after"
    EQUAL      = "equal"
    CONCURRENT = "concurrent"


@dataclass
class VectorClock:
    """Per-node logical counter map.

    ``self_id`` is the node this clock is being maintained on; it's
    the only key we increment when ``tick()`` is called. Other keys
    grow only via ``merge`` from incoming events.
    """
    self_id: str
    clock: dict[str, int] = field(default_factory=dict)

    def tick(self) -> "VectorClock":
        """Bump our own counter — call before recording a local event."""
        self.clock[self.self_id] = self.clock.get(self.self_id, 0) + 1
        return self

    def get(self, node_id: str) -> int:
        return int(self.clock.get(node_id, 0))

    def to_dict(self) -> dict[str, int]:
        # Deep-copy so callers can mutate freely.
        return dict(self.clock)

    @classmethod
    def from_dict(
        cls,
        data: dict | None,
        self_id: str,
    ) -> "VectorClock":
        clock = {}
        for k, v in (data or {}).items():
            try:
                clock[str(k)] = int(v)
            except (TypeError, ValueError):
                continue
        return cls(self_id=self_id, clock=clock)

    def merge(self, other: "VectorClock | dict") -> "VectorClock":
        """Element-wise max — absorbs the peer's knowledge of every
        node's progress."""
        peer = other.clock if isinstance(other, VectorClock) else dict(other or {})
        for k, v in peer.items():
            try:
                self.clock[str(k)] = max(self.clock.get(str(k), 0), int(v))
            except (TypeError, ValueError):
                continue
        return self

    def compare(self, other: "VectorClock | dict") -> CausalOrder:
        """Return the causal relationship between self and other."""
        peer = other.clock if isinstance(other, VectorClock) else dict(other or {})
        all_keys = set(self.clock.keys()) | set(peer.keys())
        less, more = False, False
        for k in all_keys:
            a = int(self.clock.get(k, 0))
            b = int(peer.get(k, 0))
            if a < b:
                less = True
            elif a > b:
                more = True
        if less and more:
            return CausalOrder.CONCURRENT
        if less:
            return CausalOrder.BEFORE
        if more:
            return CausalOrder.AFTER
        return CausalOrder.EQUAL

    def is_before(self, other: "VectorClock | dict") -> bool:
        return self.compare(other) is CausalOrder.BEFORE

    def is_after(self, other: "VectorClock | dict") -> bool:
        return self.compare(other) is CausalOrder.AFTER

    def is_concurrent(self, other: "VectorClock | dict") -> bool:
        return self.compare(other) is CausalOrder.CONCURRENT

    def __repr__(self) -> str:
        return f"VectorClock(self={self.self_id}, clock={self.clock})"


# ── Helpers for the federation event envelope ───────────────────


def stamp_event(envelope: dict, vc: VectorClock) -> dict:
    """Tick the vector clock and embed it in an event envelope under
    the ``vector_clock`` key. Returns the envelope for chaining."""
    vc.tick()
    envelope.setdefault("vector_clock", {})
    envelope["vector_clock"] = vc.to_dict()
    return envelope


def absorb_event(envelope: dict, vc: VectorClock) -> CausalOrder:
    """Merge a remote envelope's vector clock into ours and return
    the causal relationship between the two clocks at the moment of
    arrival.
    """
    incoming = envelope.get("vector_clock") or {}
    relation = vc.compare(incoming)
    vc.merge(incoming)
    return relation
