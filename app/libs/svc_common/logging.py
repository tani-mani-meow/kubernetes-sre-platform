"""JSON logging with trace correlation.

Every request-scoped line carries trace_id, which is what turns a Loki log line into a
click-through to its Tempo trace. Without it the logs and traces are two separate
haystacks and the "one pane of glass" story falls apart.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime

# Resolved once rather than per log record. OTel is an optional extra - tests and the CI
# overlay run without the SDK installed - so the ImportError is a real branch.
try:
    from opentelemetry import trace as _trace
except ImportError:  # pragma: no cover - exercised by the OTel-less install
    _trace = None

# Fields the LogRecord always has; anything else an author attaches is user context
# worth emitting.
_STANDARD_ATTRS = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__.keys()
) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=UTC
            ).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": record.levelname,
            "service": self.service,
            "logger": record.name,
            "message": record.getMessage(),
        }

        trace_id, span_id = _current_trace_context()
        if trace_id:
            payload["trace_id"] = trace_id
            payload["span_id"] = span_id

        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str)


def _current_trace_context() -> tuple[str | None, str | None]:
    """Active trace/span ids as hex, or (None, None) when tracing is off."""
    if _trace is None:
        return None, None

    ctx = _trace.get_current_span().get_span_context()
    if not ctx.is_valid:
        return None, None
    return format(ctx.trace_id, "032x"), format(ctx.span_id, "016x")


def configure_logging(service: str, level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # uvicorn ships its own handlers; drop them so everything is JSON on one stream.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True

    # Access logs would duplicate what the metrics middleware already records, at one
    # line per request under constant load generator traffic.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
