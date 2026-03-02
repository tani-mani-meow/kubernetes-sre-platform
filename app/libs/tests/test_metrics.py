"""Metric shape is a contract: docs/conventions.md, the Sloth specs, the Grafana
dashboards and the Rollouts AnalysisTemplate all hard-code these names and labels.
A silent rename here shows up as an empty SLO dashboard, so it gets asserted."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families
from svc_common.config import Settings
from svc_common.metrics import LATENCY_BUCKETS


@pytest.fixture
def client(monkeypatch) -> TestClient:
    from svc_common.app import create_app

    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    settings = Settings(
        service_name="probe", service_version="test", log_level="WARNING",
        otel_enabled=False, otel_endpoint="",
    )
    app: FastAPI = create_app(settings=settings)

    @app.get("/items/{item_id}")
    async def item(item_id: int):
        return {"id": item_id}

    @app.get("/boom")
    async def boom():
        raise RuntimeError("kaboom")

    return TestClient(app, raise_server_exceptions=False)


def samples(client: TestClient, metric: str) -> list:
    body = client.get("/metrics").text
    for family in text_string_to_metric_families(body):
        if family.name == metric:
            return family.samples
    return []


def test_slo_thresholds_are_exact_bucket_boundaries():
    # Latency SLIs are computed as a bucket ratio, not histogram_quantile. That is only
    # exact if every SLO threshold is itself a bucket edge.
    for threshold in (0.2, 0.4, 0.8):
        assert threshold in LATENCY_BUCKETS


def test_path_parameters_collapse_to_the_route_template(client):
    for item_id in range(1, 6):
        assert client.get(f"/items/{item_id}").status_code == 200

    routes = {s.labels["route"] for s in samples(client, "http_requests")}
    # One series for five distinct ids: raw paths would give one series per product.
    assert routes == {"/items/{item_id}"}


def test_unmatched_paths_do_not_create_series_per_path(client):
    for i in range(5):
        client.get(f"/nope/{i}")
    routes = {s.labels["route"] for s in samples(client, "http_requests")}
    assert routes == {"__unmatched__"}


def test_platform_endpoints_are_not_counted(client):
    client.get("/healthz")
    client.get("/readyz")
    client.get("/metrics")
    assert samples(client, "http_requests") == []


def test_unhandled_exceptions_count_as_500(client):
    client.get("/boom")
    codes = {s.labels["status_code"] for s in samples(client, "http_requests")}
    assert codes == {"500"}


def test_injected_faults_keep_their_real_route_label(client):
    # The fault middleware short-circuits before the router runs, so scope["route"] is
    # never set and the naive lookup returns "__unmatched__". That would put every
    # injected 500 in the same bucket as 404-scanner noise, and the per-route panels
    # would show a fault landing nowhere in particular - directly undercutting the
    # latency-catalog hypothesis that the dashboards localise a fault.
    app = client.app
    try:
        app.state.faults.update(error_rate=1.0)
        assert client.get("/items/7").status_code == 500
    finally:
        app.state.faults.reset()

    routes = {s.labels["route"] for s in samples(client, "http_requests")}
    assert routes == {"/items/{item_id}"}


def test_openmetrics_is_served_when_the_scraper_asks_for_it(client):
    # Exemplars exist only in OpenMetrics exposition. Serving the classic format
    # unconditionally would attach exemplars that are silently dropped on the way out,
    # and Grafana's "View trace" button would never appear.
    body = client.get(
        "/metrics", headers={"accept": "application/openmetrics-text"}
    ).text
    assert body.rstrip().endswith("# EOF"), "OpenMetrics exposition ends with # EOF"

    classic = client.get("/metrics", headers={"accept": "text/plain"}).text
    assert not classic.rstrip().endswith("# EOF")


def test_a_trace_id_is_attached_as_an_exemplar():
    # Grafana's "View trace" jump from a latency spike needs an exemplar on the
    # histogram bucket. Everything else was wired for this - Prometheus has
    # exemplar-storage enabled and the datasource declares exemplarTraceIdDestinations -
    # but for a while nothing actually attached one, so the button never appeared.
    from prometheus_client.openmetrics.exposition import generate_latest as om
    from svc_common.metrics import Metrics

    metrics = Metrics("probe")
    metrics.observe_request("GET", "/products/{id}", 200, 0.12,
                            trace_id="4bf92f3577b34da6a3ce929d0e0e4736")
    body = om(metrics.registry).decode()

    exemplars = [ln for ln in body.splitlines()
                 if "duration_seconds_bucket" in ln and "# {trace_id=" in ln]
    assert exemplars, "no exemplar attached to the latency histogram"
    assert "4bf92f3577b34da6a3ce929d0e0e4736" in exemplars[0]


def test_an_observation_without_a_trace_still_records():
    # Tracing is optional (OTEL_SDK_DISABLED in tests and CI), so a missing trace id
    # must not cost the observation itself.
    from prometheus_client import generate_latest
    from svc_common.metrics import Metrics

    metrics = Metrics("probe")
    metrics.observe_request("GET", "/products", 200, 0.01, trace_id=None)
    assert b"http_requests_total" in generate_latest(metrics.registry)


def test_injected_faults_are_visible_to_the_sli(client):
    # Pins the middleware ordering. The metrics layer must wrap the fault layer, or an
    # injected 500 would be returned to the caller without ever reaching a counter -
    # every chaos experiment would then inject a fault the SLI cannot see, and the
    # burn-rate alert it is supposed to trigger would never fire.
    app = client.app
    try:
        app.state.faults.update(error_rate=1.0)
        assert client.get("/items/1").status_code == 500
    finally:
        app.state.faults.reset()

    counted = {s.labels["status_code"]: s.value for s in samples(client, "http_requests")}
    assert counted.get("500") == 1.0


def test_injected_latency_is_measured_not_bypassed(client):
    app = client.app
    try:
        app.state.faults.update(latency_ms=60)
        client.get("/items/1")
    finally:
        app.state.faults.reset()

    total = next(s.value for s in samples(client, "http_request_duration_seconds")
                 if s.name.endswith("_sum"))
    # If the metrics layer sat inside the fault layer it would time only the handler and
    # miss the injected delay entirely.
    assert total >= 0.06


def test_duration_histogram_is_labelled_without_status_code(client):
    client.get("/items/1")
    labels = {s.labels["route"] for s in samples(client, "http_request_duration_seconds")}
    assert "/items/{item_id}" in labels
    # status_code on the histogram would multiply series count for no SLI benefit:
    # the latency SLI asks "how many were slow", not "how many slow ones failed".
    sample = next(s for s in samples(client, "http_request_duration_seconds")
                  if s.name.endswith("_count"))
    assert "status_code" not in sample.labels
