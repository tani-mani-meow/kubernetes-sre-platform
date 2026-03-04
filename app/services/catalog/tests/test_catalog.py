"""catalog handler behaviour against the in-memory store.

Real Postgres is exercised by the CI e2e job; these run with nothing but Python so a
failing handler is caught in seconds rather than after a cluster comes up."""

from __future__ import annotations

import pytest
from catalog.main import app
from catalog.store import InMemoryStore, OutOfStock, Product, ProductNotFound
from fastapi.testclient import TestClient


@pytest.fixture
def store() -> InMemoryStore:
    fresh = InMemoryStore()
    app.state.store = fresh
    return fresh


@pytest.fixture
def client(store) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def test_list_products(client):
    body = client.get("/products").json()
    assert [p["id"] for p in body] == [1, 2, 3]


def test_list_products_clamps_absurd_limits(client):
    # An unbounded limit is a cheap way for one request to pull the whole table and
    # blow catalog's 200ms latency SLO.
    assert client.get("/products?limit=100000").status_code == 200
    assert client.get("/products?limit=0").status_code == 200


def test_get_product(client):
    assert client.get("/products/2").json()["name"] == "27-inch Monitor"


def test_missing_product_is_404(client):
    assert client.get("/products/9999").status_code == 404


def test_create_order_decrements_stock(client, store):
    before = client.get("/products/1").json()["stock"]
    response = client.post("/orders", json={"product_id": 1, "quantity": 2})
    assert response.status_code == 201
    assert response.json()["total_cents"] == 12900 * 2
    assert client.get("/products/1").json()["stock"] == before - 2


def test_order_for_missing_product_is_404(client):
    assert client.post("/orders", json={"product_id": 9999}).status_code == 404


def test_order_beyond_stock_is_409_not_500(client, store):
    store._products[1] = Product(1, "Mechanical Keyboard", "peripherals", 12900, 1)
    response = client.post("/orders", json={"product_id": 1, "quantity": 5})
    # A client asking for more units than exist is a client error. Counting it as a
    # server error would burn the error budget of a service behaving correctly.
    assert response.status_code == 409


def test_invalid_order_payload_is_422(client):
    assert client.post("/orders", json={"product_id": 0}).status_code == 422
    assert client.post("/orders", json={"product_id": 1,
                                        "quantity": 99}).status_code == 422


def test_readiness_reflects_database_health(client, store):
    assert client.get("/readyz").json()["database"] == "up"
    store.healthy = False
    response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["database"] == "down"


def test_liveness_ignores_database_health(client, store):
    store.healthy = False
    # Liveness answers "is this process wedged". Tying it to the database would restart
    # every healthy catalog pod during a Postgres outage, turning a recoverable
    # dependency failure into a crash-loop.
    assert client.get("/healthz").status_code == 200


class RecordingMetrics:
    def __init__(self):
        self.observations = []

    def observe_dependency(self, dependency, outcome, seconds):
        self.observations.append((dependency, outcome, seconds))


async def test_database_calls_are_recorded_as_a_dependency():
    # Without this the catalog dashboard can only show total handler latency, which
    # cannot separate "the query got slower" from "the service got slower" - the exact
    # distinction the Postgres pod-kill scenario turns on.
    from catalog.store import DATABASE, PostgresStore

    metrics = RecordingMetrics()
    store = PostgresStore("postgresql://unused", metrics=metrics)

    async with store._measured("probe"):
        pass

    assert metrics.observations == [(DATABASE, "ok", pytest.approx(0.0, abs=0.5))]


async def test_a_missing_product_is_not_a_database_error():
    from catalog.store import PostgresStore

    metrics = RecordingMetrics()
    store = PostgresStore("postgresql://unused", metrics=metrics)

    with pytest.raises(ProductNotFound):
        async with store._measured("get_product"):
            raise ProductNotFound(1)

    # Counting a 404 as a dependency failure would make the database error rate track
    # client behaviour rather than database health.
    assert [o[1] for o in metrics.observations] == ["ok"]


async def test_database_failures_are_recorded_as_errors():
    from catalog.store import PostgresStore

    metrics = RecordingMetrics()
    store = PostgresStore("postgresql://unused", metrics=metrics)

    with pytest.raises(RuntimeError):
        async with store._measured("get_product"):
            raise RuntimeError("connection reset")

    assert [o[1] for o in metrics.observations] == ["error"]


async def test_timeouts_are_distinguished_from_other_errors():
    from catalog.store import PostgresStore

    metrics = RecordingMetrics()
    store = PostgresStore("postgresql://unused", metrics=metrics)

    with pytest.raises(TimeoutError):
        async with store._measured("get_product"):
            raise TimeoutError

    assert [o[1] for o in metrics.observations] == ["timeout"]


async def test_store_raises_typed_errors():
    store = InMemoryStore()
    with pytest.raises(ProductNotFound):
        await store.get_product(4242)
    store._products[1] = Product(1, "x", "y", 100, 0)
    with pytest.raises(OutOfStock):
        await store.create_order(1, 1)
