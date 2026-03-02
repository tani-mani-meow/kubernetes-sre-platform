"""Fault injection is the load-bearing mechanism for both the chaos suite and the
canary-abort demo, so its edges get tested directly rather than through a service."""

from __future__ import annotations

import random

import pytest
from svc_common.faults import FaultConfig, FaultInjector


def injector(**kwargs) -> FaultInjector:
    """Seeded RNG: fault decisions are probabilistic, tests are not."""
    return FaultInjector(FaultConfig(**kwargs), rng=random.Random(1234))


def test_no_faults_by_default():
    inj = injector()
    assert not any(inj.should_error("/products") for _ in range(200))
    assert inj.latency_for("/products") == 0.0


def test_error_rate_one_always_errors():
    inj = injector(error_rate=1.0)
    assert all(inj.should_error("/products") for _ in range(50))


@pytest.mark.parametrize("path", ["/healthz", "/readyz", "/metrics", "/internal/faults"])
def test_platform_endpoints_are_never_faulted(path):
    # A fault that failed probes would get the pod restarted, and Kubernetes healing it
    # would mask the very SLO burn the experiment is measuring.
    inj = injector(error_rate=1.0, latency_ms=5000)
    assert not inj.should_error(path)
    assert inj.latency_for(path) == 0.0


def test_error_rate_is_roughly_the_configured_fraction():
    inj = injector(error_rate=0.3)
    hits = sum(inj.should_error("/products") for _ in range(5000))
    assert 0.27 < hits / 5000 < 0.33


def test_latency_rate_gates_the_delay():
    inj = injector(latency_ms=500, latency_rate=0.5)
    delays = [inj.latency_for("/products") for _ in range(2000)]
    slow = [d for d in delays if d > 0]
    assert 0.45 < len(slow) / 2000 < 0.55
    assert all(d == 0.5 for d in slow)


def test_update_clamps_out_of_range_values():
    inj = injector()
    cfg = inj.update(error_rate=5.0, latency_ms=-10, latency_rate=-3.0)
    assert cfg.error_rate == 1.0
    assert cfg.latency_ms == 0
    assert cfg.latency_rate == 0.0


def test_update_leaves_omitted_fields_alone():
    inj = injector(error_rate=0.4, latency_ms=100)
    cfg = inj.update(latency_ms=250)
    assert cfg.error_rate == 0.4
    assert cfg.latency_ms == 250


def test_reset_clears_everything():
    inj = injector(error_rate=0.9, latency_ms=800, latency_rate=0.5)
    cfg = inj.reset()
    assert cfg == FaultConfig()


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, FaultConfig()),
        ({"FAULT_ERROR_RATE": "0.25"}, FaultConfig(error_rate=0.25)),
        ({"FAULT_LATENCY_MS": "500"}, FaultConfig(latency_ms=500)),
        # A typo in a manifest must not crash a pod at 3am; it falls back to no fault.
        ({"FAULT_ERROR_RATE": "banana"}, FaultConfig()),
        ({"FAULT_ERROR_RATE": "9"}, FaultConfig(error_rate=1.0)),
    ],
)
def test_from_env(monkeypatch, env, expected):
    for key in ("FAULT_ERROR_RATE", "FAULT_LATENCY_MS", "FAULT_LATENCY_RATE"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert FaultConfig.from_env() == expected


async def test_sleep_if_slow_returns_the_delay_applied():
    inj = injector(latency_ms=10)
    assert await inj.sleep_if_slow("/products") == pytest.approx(0.01)
    assert await inj.sleep_if_slow("/healthz") == 0.0
