"""
Phase 6 / Module AD — OpenTelemetry tracing bootstrap.

Wires the OTel SDK + optional auto-instrumentations for:
    FastAPI, SQLAlchemy, httpx, redis, asyncio loops, and
    socket.io (manual hook via ``trace_socketio_event``).

All optional deps are imported defensively — if they are missing the
service simply runs without tracing.
"""
from __future__ import annotations

import os
from typing import Any, Optional

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


_OTEL_READY = False
_TRACER: Any = None
_TRACE_MOD: Any = None


def is_ready() -> bool:
    return _OTEL_READY


def status() -> dict[str, Any]:
    return {
        "ready": _OTEL_READY,
        "exporter": os.environ.get("OTEL_EXPORTER", "otlp_http"),
        "endpoint": os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", ""),
        "service": os.environ.get("OTEL_SERVICE_NAME", "helen-server"),
    }


def setup_tracing(app: Optional[Any] = None) -> bool:
    """Initialise the OTel SDK. Returns True on success.

    Idempotent — safe to call once per process.
    """
    global _OTEL_READY, _TRACER, _TRACE_MOD
    if _OTEL_READY:
        return True

    settings = get_settings()
    service_name = os.environ.get("OTEL_SERVICE_NAME", "helen-server")
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT",
                              "http://localhost:4318")
    exporter_kind = os.environ.get("OTEL_EXPORTER", "otlp_http").lower()

    try:
        from opentelemetry import trace  # type: ignore
        from opentelemetry.sdk.resources import Resource  # type: ignore
        from opentelemetry.sdk.trace import TracerProvider  # type: ignore
        from opentelemetry.sdk.trace.export import (  # type: ignore
            BatchSpanProcessor,
        )
    except Exception as exc:                                        # pragma: no cover
        logger.info("otel: SDK not installed (%s); tracing disabled", exc)
        return False

    resource = Resource.create({
        "service.name": service_name,
        "service.namespace": "helen",
        "service.version": os.environ.get("HELEN_VERSION", "0.0.0-dev"),
        "deployment.environment": os.environ.get("HELEN_ENV", "dev"),
        "host.name": os.environ.get("HOSTNAME", ""),
    })
    provider = TracerProvider(resource=resource)

    exporter: Any = None
    try:
        if exporter_kind in ("otlp_http", "otlp"):
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # type: ignore
                OTLPSpanExporter,
            )
            exporter = OTLPSpanExporter(endpoint=endpoint.rstrip("/") + "/v1/traces")
        elif exporter_kind == "otlp_grpc":
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (  # type: ignore
                OTLPSpanExporter,
            )
            exporter = OTLPSpanExporter(endpoint=endpoint, insecure=True)
        elif exporter_kind == "jaeger":
            from opentelemetry.exporter.jaeger.thrift import (  # type: ignore
                JaegerExporter,
            )
            exporter = JaegerExporter(
                agent_host_name=os.environ.get("JAEGER_HOST", "localhost"),
                agent_port=int(os.environ.get("JAEGER_PORT", "6831")),
            )
        else:                                                       # pragma: no cover
            logger.warning("otel: unknown exporter %s", exporter_kind)
            return False
    except Exception as exc:                                        # pragma: no cover
        logger.info("otel: exporter unavailable (%s); tracing disabled", exc)
        return False

    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    _TRACE_MOD = trace
    _TRACER = trace.get_tracer("helen.server")

    # Auto-instrumentations (all optional)
    if app is not None:
        try:
            from opentelemetry.instrumentation.fastapi import (  # type: ignore
                FastAPIInstrumentor,
            )
            FastAPIInstrumentor.instrument_app(app)
        except Exception:                                           # pragma: no cover
            pass

    try:
        from opentelemetry.instrumentation.sqlalchemy import (      # type: ignore
            SQLAlchemyInstrumentor,
        )
        SQLAlchemyInstrumentor().instrument()
    except Exception:                                               # pragma: no cover
        pass

    try:
        from opentelemetry.instrumentation.httpx import (           # type: ignore
            HTTPXClientInstrumentor,
        )
        HTTPXClientInstrumentor().instrument()
    except Exception:                                               # pragma: no cover
        pass

    try:
        from opentelemetry.instrumentation.redis import (           # type: ignore
            RedisInstrumentor,
        )
        RedisInstrumentor().instrument()
    except Exception:                                               # pragma: no cover
        pass

    _OTEL_READY = True
    logger.info("otel: tracing ready (service=%s exporter=%s endpoint=%s)",
                service_name, exporter_kind, endpoint)
    return True


def trace_socketio_event(event: str, **attrs: Any):
    """Context-manager helper to span a socket.io event."""
    if not _OTEL_READY or _TRACER is None:
        from contextlib import nullcontext
        return nullcontext()
    span = _TRACER.start_as_current_span(f"socketio.{event}")
    if attrs:
        # Add attrs once we enter the span; manual fallback via wrapper:
        class _Wrap:
            def __enter__(self_inner):
                self_inner._span = span.__enter__()
                for k, v in attrs.items():
                    try:
                        self_inner._span.set_attribute(k, v)
                    except Exception:
                        pass
                return self_inner._span
            def __exit__(self_inner, *a):
                return span.__exit__(*a)
        return _Wrap()
    return span


def current_trace_id() -> Optional[str]:
    if not _OTEL_READY or _TRACE_MOD is None:
        return None
    try:
        sc = _TRACE_MOD.get_current_span().get_span_context()
        if not sc.is_valid:
            return None
        return f"{sc.trace_id:032x}"
    except Exception:                                               # pragma: no cover
        return None
