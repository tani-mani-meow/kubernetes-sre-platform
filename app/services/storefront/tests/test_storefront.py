"""storefront behaviour under dependency failure.

These tests encode the design decision the incident evidence depends on: a recs failure
degrades the page, a catalog failure fails it. If recs failures propagated as 500s, the
"slow dependency" chaos scenario would be indistinguishable from a hard outage on every
dashboard in the repo."""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient
from storefront.main import app
from svc_common import DependencyClient

PRODUCT = {"id": 1, "name": "Mechanical Keyboard", "category": "peripherals",
           "price_cents": 12900, "stock": 40}
RECS = {"product_id": 1, "recommendations": [{"product_id": 3, "score": 0.9,
                                              "reason": "similar"}],
        "latency_ms": 250, "slow_path": False}


def stub(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def ok_catalog(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/products":
        return httpx.Response(200, json=[PRODUCT])
    if request.url.path == "/products/1":
        return httpx.Response(200, json=PRODUCT)
    if request.url.path == "/products/999":
        return httpx.Response(404, json={"detail": "product not found"})
    if request.url.path == "/orders":
        return httpx.Response(201, json={"id": 7, "product_id": 1, "quantity": 1,
                                         "total_cents": 12900})
    return httpx.Response(404)


def ok_recs(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=RECS)


@pytest.fixture
def client():
    def build(catalog_handler=ok_catalog, recs_handler=ok_recs) -> TestClient:
        app.state.catalog = DependencyClient(
            "catalog", "http://catalog", app.state.metrics,
            transport=stub(catalog_handler))
        app.state.recs = DependencyClient(
            "recs", "http://recs", app.state.metrics, transport=stub(recs_handler))
        return TestClient(app, raise_server_exceptions=False)

    return build


def test_product_detail_fans_out_to_both_dependencies(client):
    response = client().get("/products/1")
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Mechanical Keyboard"
    assert body["recommendations"][0]["product_id"] == 3
    assert body["recommendations_degraded"] is False


def test_recs_failure_degrades_instead_of_failing_the_page(client):
    def recs_down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "injected fault"})

    response = client(recs_handler=recs_down).get("/products/1")
    assert response.status_code == 200
    assert response.json()["recommendations"] == []
    assert response.json()["recommendations_degraded"] is True


def test_recs_timeout_degrades_too(client):
    def recs_hangs(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    response = client(recs_handler=recs_hangs).get("/products/1")
    assert response.status_code == 200
    assert response.json()["recommendations_degraded"] is True


def test_catalog_failure_fails_the_request_as_503(client):
    def catalog_down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "injected fault"})

    response = client(catalog_handler=catalog_down).get("/products/1")
    # 503, not 500: the dependency is unavailable, storefront itself is fine. Both burn
    # the availability budget, but the status code is what an on-call engineer reads
    # first on the dashboard.
    assert response.status_code == 503


def test_catalog_404_is_passed_through_not_turned_into_5xx(client):
    response = client().get("/products/999")
    # A client asking for a product that does not exist must not burn the error budget.
    assert response.status_code == 404


def test_order_conflict_is_passed_through(client):
    def out_of_stock(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/orders":
            return httpx.Response(409, json={"detail": "insufficient stock"})
        return ok_catalog(request)

    response = client(catalog_handler=out_of_stock).post(
        "/orders", json={"product_id": 1, "quantity": 3})
    assert response.status_code == 409
    assert response.json()["detail"] == "insufficient stock"


def test_order_succeeds(client):
    response = client().post("/orders", json={"product_id": 1, "quantity": 1})
    assert response.status_code == 201
    assert response.json()["id"] == 7


def test_readiness_tolerates_recs_being_down(client):
    def recs_down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    def catalog_healthy(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok"})
        return ok_catalog(request)

    response = client(catalog_handler=catalog_healthy,
                      recs_handler=recs_down).get("/readyz")
    # Reporting NotReady here would pull every pod out of the Service and turn a
    # partial degradation into a total outage.
    assert response.status_code == 200
    assert response.json()["recs"] == "down"


def test_readiness_fails_when_catalog_is_down(client):
    def catalog_down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    response = client(catalog_handler=catalog_down).get("/readyz")
    assert response.status_code == 503
    assert response.json()["catalog"] == "down"


def test_injected_error_rate_produces_500s(client):
    c = client()
    try:
        app.state.faults.update(error_rate=1.0)
        assert c.get("/products/1").status_code == 500
        # The probe endpoints must survive it - this is what keeps a fault-injected pod
        # from being restarted out from under the experiment.
        assert c.get("/healthz").status_code == 200
    finally:
        app.state.faults.reset()
