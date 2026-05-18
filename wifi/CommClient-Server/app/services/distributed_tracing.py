"""Distributed tracing — propagate trace_id across relay hops.

A debugging aid: every request gets a (trace_id, span_id) tuple. The
relay layer adds the headers ``X-Helen-Trace-Id`` and
``X-Helen-Span-Id`` to outbound calls, and each receiver appends a
new span with its node_id + timing.

Spans are kept in memory (bounded ring) and exposed via:

  GET /api/admin/peers/tracing/recent
  GET /api/admin/peers/tracing/{trace_id}

Lighter than full OpenTelemetry — no external collector, no
SpanProcessor. Just enough to debug "why did this request take 8
seconds across 4 hops".
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field, asdict
from typing import Optional


def _i(env: str, default: int) -> int:
    try:
        return int(os.environ.get(env) or default)
    except (TypeError, ValueError):
        return default


HEADER_TRACE_ID = "X-Helen-Trace-Id"
HEADER_SPAN_ID  = "X-Helen-Span-Id"
HEADER_PARENT   = "X-Helen-Parent-Span-Id"

MAX_SPANS = _i("HELEN_TRACING_MAX_SPANS", 5000)


@dataclass
class Span:
    trace_id:    str
    span_id:     str
    parent_id:   str
    name:        str
    node_id:     str
    started_at:  float
    finished_at: float = 0.0
    tags:        dict = field(default_factory=dict)

    @property
    def duration_ms(self) -> float:
        end = self.finished_at or time.time()
        return round((end - self.started_at) * 1000.0, 3)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["duration_ms"] = self.duration_ms
        return d


def new_trace_id() -> str:
    return uuid.uuid4().hex


def new_span_id() -> str:
    return uuid.uuid4().hex[:16]


class TracingRegistry:
    _singleton: "TracingRegistry | None" = None

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._spans: deque = deque(maxlen=MAX_SPANS)
        self._open: dict[str, Span] = {}

    @classmethod
    def instance(cls) -> "TracingRegistry":
        if cls._singleton is None:
            cls._singleton = TracingRegistry()
        return cls._singleton

    def start_span(self, name: str,
                   *, trace_id: str | None = None,
                   parent_id: str = "",
                   tags: dict | None = None) -> Span:
        try:
            from app.services.discovery_service import get_server_id
            node_id = get_server_id() or "anon"
        except Exception:
            node_id = "anon"
        s = Span(
            trace_id=trace_id or new_trace_id(),
            span_id=new_span_id(),
            parent_id=parent_id,
            name=name,
            node_id=node_id,
            started_at=time.time(),
            tags=dict(tags or {}),
        )
        with self._lock:
            self._open[s.span_id] = s
        return s

    def finish_span(self, span_id: str, *,
                    extra_tags: dict | None = None) -> Optional[Span]:
        with self._lock:
            s = self._open.pop(span_id, None)
        if s is None:
            return None
        s.finished_at = time.time()
        if extra_tags:
            s.tags.update(extra_tags)
        with self._lock:
            self._spans.append(s)
        return s

    # ── Diagnostics ───────────────────────────────────────

    def recent(self, limit: int = 50) -> list[dict]:
        with self._lock:
            return [s.to_dict() for s in list(self._spans)[-int(limit):]]

    def by_trace(self, trace_id: str) -> list[dict]:
        with self._lock:
            return sorted(
                (s.to_dict() for s in self._spans
                 if s.trace_id == trace_id),
                key=lambda d: d["started_at"],
            )

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "open_spans":     len(self._open),
                "completed_spans": len(self._spans),
                "max_spans":      MAX_SPANS,
            }


def get_tracing() -> TracingRegistry:
    return TracingRegistry.instance()


# ── Header helpers ─────────────────────────────────────────────


def headers_for_outbound(trace_id: str, parent_span_id: str) -> dict[str, str]:
    return {
        HEADER_TRACE_ID:  trace_id,
        HEADER_PARENT:    parent_span_id,
        HEADER_SPAN_ID:   new_span_id(),
    }


def parse_inbound(headers: dict | None) -> tuple[str, str]:
    """Returns (trace_id, parent_span_id). New trace if missing."""
    if not headers:
        return new_trace_id(), ""
    norm = {k.lower(): v for k, v in headers.items()}
    trace = str(norm.get(HEADER_TRACE_ID.lower(), "")) or new_trace_id()
    parent = str(norm.get(HEADER_PARENT.lower(), ""))
    return trace, parent
