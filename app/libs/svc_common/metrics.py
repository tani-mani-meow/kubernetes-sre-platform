"""Prometheus metrics.

Hand-written rather than pulled from an instrumentation library, for two reasons that
matter to this project specifically:

  * label cardinality is under our control - `route` is always the route *template*,
    so /products/17 and /products/18 share one series instead of creating one each
  * buckets line up exactly with the SLO thresholds, so a latency SLI is a bucket
    ratio (a count) rather than a histogram_quantile estimate (an interpolation)
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from prometheus_client.openmetrics.exposition import (
    CONTENT_TYPE_LATEST as OPENMETRICS_CONTENT_TYPE,
)
from prometheus_client.openmetrics.exposition import (
    generate_latest as openmetrics_generate,
)
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Match

from .faults import EXEMPT_PREFIXES

# Resolved once rather than per request: this is looked up on every response, and OTel
# is an optional extra, so the ImportError is a real branch rather than a formality.
try:
    from opentelemetry import trace as _trace
except ImportError:  # pragma: no cover - exercised by the OTel-less install
    _trace = None

#: Shared across services so one recording rule works for all of them. The SLO
#: thresholds (0.2 catalog, 0.4 storefront, 0.8 recs) are bucket boundaries on purpose.
LATENCY_BUCKETS = (
    0.005, 0.01, 0.025, 0.05, 0.1, 0.2, 0.25, 0.4, 0.5, 0.8, 1.0, 2.5, 5.0, 10.0,
)

DEPENDENCY_BUCKETS = (
    0.005, 0.01, 0.025, 0.05, 0.1, 0.2, 0.25, 0.4, 0.5, 0.8, 1.0, 2.5, 5.0,
)


class Metrics:
    """Metric handles bound to one registry.

    A per-instance registry (rather than prometheus_client's global default) keeps
    tests isolated - otherwise the second test to build an app hits "Duplicated
    timeseries in CollectorRegistry".
    """

    def __init__(self, service: str, registry: CollectorRegistry | None = None) -> None:
        self.service = service
        self.registry = registry or CollectorRegistry()

        self.requests = Counter(
            "http_requests_total",
            "HTTP requests handled, by outcome.",
            ["service", "method", "route", "status_code"],
            registry=self.registry,
        )
        self.duration = Histogram(
            "http_request_duration_seconds",
            "HTTP request handling duration.",
            ["service", "method", "route"],
            buckets=LATENCY_BUCKETS,
            registry=self.registry,
        )
        self.in_flight = Gauge(
            "http_requests_in_flight",
            "HTTP requests currently being handled.",
            ["service"],
            registry=self.registry,
        )
        self.dependency_requests = Counter(
            "dependency_requests_total",
            "Outbound calls to a dependency, by outcome.",
            ["service", "dependency", "outcome"],
            registry=self.registry,
        )
        self.dependency_duration = Histogram(
            "dependency_request_duration_seconds",
            "Outbound dependency call duration.",
            ["service", "dependency"],
            buckets=DEPENDENCY_BUCKETS,
            registry=self.registry,
        )
        self.faults = Counter(
            "fault_injected_total",
            "Faults injected into request handling.",
            ["service", "kind"],
            registry=self.registry,
        )

    def observe_request(self, method: str, route: str, status_code: int,
                        seconds: float, trace_id: str | None = None) -> None:
        self.requests.labels(self.service, method, route, str(status_code)).inc()
        histogram = self.duration.labels(self.service, method, route)
        if trace_id:
            # An exemplar is what turns a latency spike on a Grafana panel into a click
            # through to the trace that caused it. Prometheus only parses exemplars from
            # OpenMetrics exposition, which `render()` negotiates.
            try:
                histogram.observe(seconds, exemplar={"trace_id": trace_id})
            except ValueError:
                # Exemplar labels have a combined length limit; drop the exemplar
                # rather than lose the observation.
                histogram.observe(seconds)
        else:
            histogram.observe(seconds)

    def observe_dependency(self, dependency: str, outcome: str, seconds: float) -> None:
        self.dependency_requests.labels(self.service, dependency, outcome).inc()
        self.dependency_duration.labels(self.service, dependency).observe(seconds)

    def record_fault(self, kind: str) -> None:
        self.faults.labels(self.service, kind).inc()

    def render(self, accept: str = "") -> Response:
        """Prometheus exposition, in OpenMetrics format when the scraper asks for it.

        Content negotiation is not optional here: exemplars exist only in OpenMetrics,
        so serving the classic text format unconditionally would attach exemplars that
        are then silently dropped on the way out - the metric would look right and the
        "View trace" button would never appear.
        """
        if "application/openmetrics-text" in accept:
            return Response(openmetrics_generate(self.registry),
                            media_type=OPENMETRICS_CONTENT_TYPE)
        return Response(generate_latest(self.registry), media_type=CONTENT_TYPE_LATEST)


def route_template(request: Request) -> str:
    """The matched route pattern, or a low-cardinality stand-in.

    Unmatched paths collapse to a single "__unmatched__" series: a 404 scanner hitting
    random URLs must not be able to create unbounded series in Prometheus.
    """
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    if path:
        return path

    # scope["route"] is set by the router, which never runs when a middleware
    # short-circuits the request - which is exactly what fault injection does. Without
    # this fallback every injected 500 would be attributed to "__unmatched__", making
    # injected faults indistinguishable from 404-scanner noise on the per-route panels
    # and undercutting the claim that the dashboards localise a fault.
    return _match_route(request)


def _match_route(request: Request) -> str:
    app = request.scope.get("app")
    routes = getattr(app, "routes", None)
    if not routes:
        return "__unmatched__"

    best = "__unmatched__"
    for route in routes:
        try:
            match, _ = route.matches(request.scope)
        except Exception:
            continue
        if match == Match.FULL:
            return getattr(route, "path", best)
        if match == Match.PARTIAL and best == "__unmatched__":
            # A method mismatch still identifies the route; keep looking for a full
            # match, but do not fall back to "__unmatched__" if none arrives.
            best = getattr(route, "path", best)
    return best


def make_metrics_middleware(metrics: Metrics) -> Callable[..., Awaitable[Response]]:
    """Starlette HTTP middleware that records every non-exempt request."""

    async def middleware(request: Request, call_next) -> Response:
        path = request.url.path
        if path.startswith(EXEMPT_PREFIXES):
            # Probe and scrape traffic is not user traffic. Counting it would inflate
            # every SLI with requests that are guaranteed to succeed.
            return await call_next(request)

        metrics.in_flight.labels(metrics.service).inc()
        started = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            elapsed = time.perf_counter() - started
            metrics.in_flight.labels(metrics.service).dec()
            metrics.observe_request(request.method, route_template(request),
                                    status_code, elapsed, trace_id=_current_trace_id())

    return middleware


def _current_trace_id() -> str | None:
    """Hex trace id of the active span, for attaching as a histogram exemplar."""
    if _trace is None:
        return None
    ctx = _trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else None
