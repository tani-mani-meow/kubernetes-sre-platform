"""OpenTelemetry tracing setup.

Traces and logs go through the OTel Collector to Tempo and Loki. Metrics deliberately
do not - they are scraped straight off each pod by Prometheus. See
docs/adr/0002-metrics-bypass-the-otel-collector.md: Argo Rollouts' canary analysis needs
to tell canary pods from stable ones by the rollouts-pod-template-hash pod label, and a
collector gateway erases pod identity unless you fight the k8sattributes processor.

Everything here is optional. With OTEL_SDK_DISABLED=true (tests, CI) the setup is a
no-op and the services still work.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI

from .config import Settings

_log = logging.getLogger(__name__)


def setup_tracing(app: FastAPI, settings: Settings) -> bool:
    """Wire up OTel tracing. Returns True if tracing is active.

    Never raises: a broken collector endpoint must not stop a service from starting.
    A service that refuses to boot because its telemetry backend is down is a worse
    outage than one running blind.
    """
    if not settings.otel_enabled:
        _log.info("tracing disabled via OTEL_SDK_DISABLED")
        return False
    if not settings.otel_endpoint:
        _log.info("tracing disabled: no OTEL_EXPORTER_OTLP_ENDPOINT set")
        return False

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        _log.warning("tracing disabled: opentelemetry packages not installed")
        return False

    resource = Resource.create({
        "service.name": settings.service_name,
        "service.version": settings.service_version,
    })
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(endpoint=f"{settings.otel_endpoint.rstrip('/')}/v1/traces")
        )
    )
    trace.set_tracer_provider(provider)

    FastAPIInstrumentor.instrument_app(
        app,
        # Probe and scrape traffic would otherwise be ~half of all spans in Tempo.
        excluded_urls="healthz,readyz,metrics,internal",
    )
    HTTPXClientInstrumentor().instrument()

    _log.info("tracing enabled", extra={"endpoint": settings.otel_endpoint})
    return True
