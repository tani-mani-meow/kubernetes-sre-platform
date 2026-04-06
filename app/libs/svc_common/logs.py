"""Shipping logs to the OTel Collector.

The services write JSON to stdout *and* export the same records over OTLP. Two sinks,
deliberately:

  stdout   survives everything. `kubectl logs` works when the collector is down, when
           Loki is down, and during the first seconds of a pod's life before any
           exporter has connected. It is what you read while debugging the telemetry
           pipeline itself.

  OTLP     is what reaches Loki, and it is the path that carries trace context. The
           SDK's LoggingHandler attaches trace_id and span_id from the active span to
           every record, which is what makes a Loki line clickable through to its Tempo
           trace. That correlation is the whole reason logs are in this project.

The alternative - a node-level agent tailing container stdout - is what most clusters
actually run, and it has the advantage of catching output from processes that die before
flushing. It was not used here because it needs a DaemonSet collector alongside the
gateway, and because scraping stdout back out of a JSON file to recover the trace_id the
process already knew is a longer way round to the same place. The trade-off is recorded
in docs/adr/0002-metrics-bypass-the-otel-collector.md's neighbourhood: a log line emitted
during a hard crash may not be exported.
"""

from __future__ import annotations

import logging

from .config import Settings

_log = logging.getLogger(__name__)


def setup_log_export(settings: Settings) -> bool:
    """Attach an OTLP log handler to the root logger. Returns True if it is active.

    Never raises. A telemetry backend being unreachable must not stop a service from
    starting - the same rule tracing.py follows, and for the same reason: a service that
    refuses to boot because its logging pipeline is down is a worse outage than one
    running with degraded observability.
    """
    if not settings.otel_enabled or not settings.otel_endpoint:
        return False

    try:
        from opentelemetry._logs import set_logger_provider
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        from opentelemetry.sdk.resources import Resource
    except ImportError:
        _log.warning("log export disabled: opentelemetry logging packages not installed")
        return False

    provider = LoggerProvider(
        resource=Resource.create({
            # Must match the `service` label on the metrics and the service.name on
            # spans, or a service arrives in Loki under a different name than it has on
            # every dashboard. docs/conventions.md fixes this to SERVICE_NAME.
            "service.name": settings.service_name,
            "service.version": settings.service_version,
        })
    )
    provider.add_log_record_processor(
        BatchLogRecordProcessor(
            OTLPLogExporter(endpoint=f"{settings.otel_endpoint.rstrip('/')}/v1/logs")
        )
    )
    set_logger_provider(provider)

    handler = LoggingHandler(level=logging.INFO, logger_provider=provider)
    # Added alongside the existing stdout handler rather than replacing it.
    logging.getLogger().addHandler(handler)

    _log.info("log export enabled", extra={"endpoint": settings.otel_endpoint})
    return True
