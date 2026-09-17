"""OpenTelemetry setup for the Thymira API boundary."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

if TYPE_CHECKING:
    from fastapi import FastAPI
    from opentelemetry.trace import TracerProvider as TraceProvider

__all__ = ["TelemetryConfigurationError", "install_tracing"]

_DEFAULT_SERVICE_NAME = "thymira-api"
_DEFAULT_EXPORTER = "none"
_TRACING_INSTALLED_STATE_KEY = "thymira_tracing_installed"


class TelemetryConfigurationError(RuntimeError):
    """Raised when the API receives an unsupported telemetry configuration."""


def install_tracing(
    app: FastAPI,
    *,
    tracer_provider: TraceProvider | None = None,
) -> None:
    """Instrument one FastAPI application with one server span per request.

    The default exporter is a no-op. Set ``OTEL_TRACES_EXPORTER=otlp`` to send spans to the
    endpoint configured by the standard OpenTelemetry environment variables. Tests may provide
    an in-memory tracer provider without changing process-global OpenTelemetry state.
    """
    if getattr(app.state, _TRACING_INSTALLED_STATE_KEY, False):
        return

    provider = tracer_provider or _provider_from_environment()
    FastAPIInstrumentor.instrument_app(app, tracer_provider=provider)
    setattr(app.state, _TRACING_INSTALLED_STATE_KEY, True)


def _provider_from_environment() -> TracerProvider | None:
    """Build the configured exporter provider, or return ``None`` for no-op tracing."""
    exporter = os.environ.get("OTEL_TRACES_EXPORTER", _DEFAULT_EXPORTER).strip().lower()
    if exporter in {"", "none"}:
        return None
    if exporter != "otlp":
        raise TelemetryConfigurationError("OTEL_TRACES_EXPORTER must be 'none' or 'otlp'.")

    service_name = os.environ.get("OTEL_SERVICE_NAME", _DEFAULT_SERVICE_NAME).strip()
    if not service_name:
        raise TelemetryConfigurationError("OTEL_SERVICE_NAME must not be empty.")
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    return provider
