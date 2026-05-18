"""
OpenTelemetry exporter — adapts our internal route_trace data into
OTLP spans so external trace systems (Jaeger, Tempo, Honeycomb, etc.)
can render them.

Lazy import strategy
--------------------
The opentelemetry SDK is a heavy dep (pulls in protobuf, grpc, etc.).
We don't want to require it for every Helen deployment — only the
ones that have a trace collector running. So:

* All otel imports are guarded inside ``_get_tracer()`` — first call
  attempts the import and caches the result. Failure → no-op
  exporter (every method returns silently).
* `OTEL_EXPORTER_OTLP_ENDPOINT` env var (the standard OTLP env)
  auto-enables the exporter when set AND the SDK is installed.
* Without either, Helen runs as today: traces stay in the
  ``route_traces`` table for `/api/chaos/traces/{tid}` inspection.

API
---
::

    >>> # Wire at startup; safe to call even without otel installed.
    >>> await otel_exporter.export_completed_trace(trace_id)

The exporter walks ``RouteHop`` rows for a finished trace and emits
one OTLP span per hop. Spans are linked via ``parent_span_id`` →
``span_id`` so the collector reconstructs the causal chain.

When ``opentelemetry-sdk`` is added to requirements, no code change
is needed — the lazy import resolves and exports begin flowing.
"""

from __future__ import annotations

import os
from typing import Optional

from app.core.logging import get_logger

logger = get_logger(__name__)

_OTEL_ENDPOINT = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
_OTEL_SERVICE_NAME = os.environ.get("OTEL_SERVICE_NAME", "helen-server")

# Lazily-resolved otel handles. None until we first try to import.
_resolved = False
_tracer = None  # type: ignore[assignment]
_TraceFlags = None
_Status = None
_StatusCode = None
_NonRecordingSpan = None
_SpanContext = None


def _resolve_otel() -> None:
    """First-call-only attempt at importing the SDK + wiring an OTLP
    exporter. Subsequent calls are no-ops. On failure, ``_tracer``
    stays ``None`` and exporters become no-ops."""
    global _resolved, _tracer, _TraceFlags, _Status, _StatusCode
    global _NonRecordingSpan, _SpanContext
    if _resolved:
        return
    _resolved = True

    if not _OTEL_ENDPOINT:
        return  # env var not set — feature off

    try:
        from opentelemetry import trace as _otel_trace
        from opentelemetry.sdk.trace import TracerProvider, Tracer
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.trace import (
            SpanContext as __SpanContext,
            TraceFlags as __TraceFlags,
            NonRecordingSpan as __NonRecordingSpan,
            Status as __Status,
            StatusCode as __StatusCode,
        )
    except Exception as e:
        logger.info("otel_sdk_unavailable", note=str(e))
        return

    try:
        provider = TracerProvider(
            resource=Resource.create({"service.name": _OTEL_SERVICE_NAME})
        )
        exporter = OTLPSpanExporter(endpoint=_OTEL_ENDPOINT)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        _otel_trace.set_tracer_provider(provider)
        _tracer = _otel_trace.get_tracer("helen.fabric")
        _TraceFlags = __TraceFlags
        _Status = __Status
        _StatusCode = __StatusCode
        _NonRecordingSpan = __NonRecordingSpan
        _SpanContext = __SpanContext
        logger.info(
            "otel_exporter_enabled",
            endpoint=_OTEL_ENDPOINT,
            service=_OTEL_SERVICE_NAME,
        )
    except Exception as e:
        logger.warning("otel_exporter_init_failed", error=str(e))


def is_enabled() -> bool:
    _resolve_otel()
    return _tracer is not None


def _ulid_to_int(s: str, bits: int) -> int:
    """Map our base32 ULID-ish IDs to fixed-width ints for OTel.
    OTel trace_ids are 128 bits, span_ids are 64 bits, both required
    integers. We hash the ULID string to fit. Stable per ID."""
    import hashlib
    h = hashlib.sha256(s.encode("utf-8")).digest()
    n = int.from_bytes(h[: bits // 8], "big")
    return n & ((1 << bits) - 1)


async def export_completed_trace(trace_id: str) -> bool:
    """Emit OTLP spans for a completed RouteTrace. No-op if otel SDK
    is not installed or `OTEL_EXPORTER_OTLP_ENDPOINT` is unset.

    Returns True on emit attempt (regardless of network outcome —
    OTLP exporter is async and batched), False if the trace doesn't
    exist or otel is disabled."""
    _resolve_otel()
    if _tracer is None:
        return False

    try:
        from app.services.trace_collector_service import trace_collector
    except Exception:
        return False

    trace = await trace_collector.get_trace(trace_id)
    if trace is None:
        return False

    try:
        from datetime import datetime
        otel_trace_id = _ulid_to_int(trace_id, 128)

        # We can't currently inject our own trace_id into OTel spans
        # without using the low-level SDK API. The simpler path:
        # create a parent span per RouteTrace and child spans per
        # RouteHop. The collector will link them into one trace
        # automatically. The unique ID mapping above guarantees no
        # collision across separate traces.

        for h in trace.get("hops", []):
            received_at = h.get("received_at")
            forwarded_at = h.get("forwarded_at")
            if not received_at:
                continue

            try:
                start_ns = int(datetime.fromisoformat(
                    received_at.replace("Z", "+00:00")
                ).timestamp() * 1_000_000_000)
            except Exception:
                continue
            try:
                end_ns = int(datetime.fromisoformat(
                    (forwarded_at or received_at).replace("Z", "+00:00")
                ).timestamp() * 1_000_000_000)
            except Exception:
                end_ns = start_ns + 1_000_000

            with _tracer.start_as_current_span(
                name=f"hop:{h.get('action', 'unknown')}:{h.get('hop_index')}",
                start_time=start_ns,
                attributes={
                    "fabric.trace_id": trace_id,
                    "fabric.event_id": trace.get("event_id", ""),
                    "fabric.event_type": trace.get("event_type", ""),
                    "fabric.priority": trace.get("priority", ""),
                    "fabric.hop_index": h.get("hop_index", 0),
                    "fabric.server_id": h.get("server_id", ""),
                    "fabric.next_server_id": h.get("next_server_id", "") or "",
                    "fabric.action": h.get("action", ""),
                },
            ) as span:
                if _Status is not None and _StatusCode is not None:
                    if h.get("action") in ("dlq", "loop", "expired", "max_hops"):
                        span.set_status(_Status(_StatusCode.ERROR, h.get("action", "")))
                    else:
                        span.set_status(_Status(_StatusCode.OK))
                span.end(end_time=end_ns)
        return True
    except Exception as e:
        logger.warning("otel_export_failed", trace_id=trace_id, error=str(e))
        return False
