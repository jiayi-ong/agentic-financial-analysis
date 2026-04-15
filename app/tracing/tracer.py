"""
OpenTelemetry tracing setup.

Provides a single tracer for the application.  Spans are exported to:
  - ConsoleSpanExporter (local development, always enabled)
  - Google Cloud Trace   (Cloud Run, enabled via ENABLE_CLOUD_TRACE=true)

ADK emits its own OTel spans automatically; this module adds application-level
spans around orchestrator steps and tool calls.

Usage:
    from app.tracing.tracer import get_tracer

    tracer = get_tracer()
    with tracer.start_as_current_span("my_span") as span:
        span.set_attribute("ticker", "AAPL")
        ...
"""

from __future__ import annotations

import logging

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

logger = logging.getLogger(__name__)

_TRACER_NAME = "financial_analyst"
_initialized = False


def _setup_tracing(enable_cloud_trace: bool = False) -> None:
    """Configure the global OTel TracerProvider (called once at startup)."""
    global _initialized
    if _initialized:
        return

    resource = Resource.create({"service.name": "financial-analyst"})
    provider = TracerProvider(resource=resource)

    # Always add console exporter (helpful for local debugging)
    provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))

    if enable_cloud_trace:
        try:
            from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter

            provider.add_span_processor(
                BatchSpanProcessor(CloudTraceSpanExporter())
            )
            logger.info("Google Cloud Trace exporter enabled.")
        except ImportError:
            logger.warning(
                "opentelemetry-exporter-gcp-trace not installed; "
                "Cloud Trace export disabled."
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to initialise Cloud Trace exporter: %s", exc)

    trace.set_tracer_provider(provider)
    _initialized = True
    logger.info("OpenTelemetry tracing initialised.")


def init_tracing(enable_cloud_trace: bool = False) -> None:
    """Initialise tracing.  Call once at application startup."""
    _setup_tracing(enable_cloud_trace)


def get_tracer() -> trace.Tracer:
    """Return the application tracer.  Safe to call before init_tracing()."""
    return trace.get_tracer(_TRACER_NAME)
