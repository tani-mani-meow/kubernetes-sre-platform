"""recs is the deliberately slow dependency; its latency shape is a feature, so it is
what gets tested."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from recs.main import _recommendations_for, _simulated_work_seconds, app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def test_returns_recommendations(client):
    body = client.get("/recommendations/1").json()
    assert body["product_id"] == 1
    assert len(body["recommendations"]) == 3
    assert all(0.0 <= r["score"] <= 1.0 for r in body["recommendations"])


def test_recommendations_are_deterministic_per_product():
    # Stable output across replicas matters during a canary: if stable and canary pods
    # disagreed on the same product, a reviewer could not tell an analysis failure from
    # ordinary response variation.
    first = _recommendations_for(7)
    assert first == _recommendations_for(7)
    assert first != _recommendations_for(8)


def test_a_product_never_recommends_itself():
    for product_id in range(1, 13):
        ids = [r.product_id for r in _recommendations_for(product_id, count=5)]
        assert product_id not in ids


def test_limit_is_clamped(client):
    assert len(client.get("/recommendations/1?limit=50").json()["recommendations"]) == 10
    assert len(client.get("/recommendations/1?limit=0").json()["recommendations"]) == 1


def test_latency_has_a_long_tail():
    # Without a tail, every request lands in one histogram bucket and a p99 latency SLO
    # is indistinguishable from a p50 one.
    #
    # 2%, not 5%: the tail reaches storefront through the 15% of traffic that fans out,
    # and every tail request breaches storefront's 400ms SLO. At 5% that is 0.75% of
    # storefront requests against a 1% budget - a 0.75 burn rate with nothing wrong.
    samples = [_simulated_work_seconds() for _ in range(8000)]
    slow = [seconds for seconds, tail in samples if tail]
    fast = [seconds for seconds, tail in samples if not tail]

    assert 0.01 < len(slow) / len(samples) < 0.035, "tail rate should be near 2%"
    assert min(slow) > max(fast), "tail requests must be strictly slower"


def test_baseline_latency_is_jittered_around_the_configured_value():
    samples = [seconds for seconds, tail in
               (_simulated_work_seconds() for _ in range(2000)) if not tail]
    assert 0.008 <= min(samples) < 0.010
    assert 0.010 < max(samples) <= 0.012
