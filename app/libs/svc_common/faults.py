"""Fault injection.

This is the load-bearing piece of the whole project. The same two knobs drive:

  * chaos experiments  - the harness flips them at runtime via PUT /internal/faults
  * the broken canary  - a Git commit sets FAULT_ERROR_RATE on the Rollout's pod
                         template, which is what makes Argo Rollouts' analysis abort

Health, readiness and metrics endpoints are never faulted. A fault that failed probes
would get the pod restarted, and Kubernetes healing it would mask the SLO burn the
experiment is trying to measure.
"""

from __future__ import annotations

import asyncio
import random
import threading
from dataclasses import asdict, dataclass, replace

from .config import env_float

#: Paths that must keep working no matter what is injected.
EXEMPT_PREFIXES = ("/healthz", "/readyz", "/metrics", "/internal/")


@dataclass(frozen=True)
class FaultConfig:
    error_rate: float = 0.0
    latency_ms: int = 0
    latency_rate: float = 1.0

    @classmethod
    def from_env(cls) -> FaultConfig:
        return cls(
            error_rate=env_float("FAULT_ERROR_RATE", 0.0, 0.0, 1.0),
            latency_ms=int(env_float("FAULT_LATENCY_MS", 0.0, 0.0, 60_000.0)),
            latency_rate=env_float("FAULT_LATENCY_RATE", 1.0, 0.0, 1.0),
        )

    def as_dict(self) -> dict[str, float | int]:
        return asdict(self)


class FaultInjector:
    """Holds the current fault config and decides what to do with a request.

    Reads are lock-free (dataclass reference swap is atomic under the GIL); only writes
    take the lock. Request handling happens on every request, config changes happen a
    handful of times per demo.
    """

    def __init__(self, config: FaultConfig | None = None,
                 rng: random.Random | None = None) -> None:
        self._config = config or FaultConfig()
        self._lock = threading.Lock()
        self._rng = rng or random.Random()

    @property
    def config(self) -> FaultConfig:
        return self._config

    def update(self, *, error_rate: float | None = None, latency_ms: int | None = None,
               latency_rate: float | None = None) -> FaultConfig:
        with self._lock:
            changes: dict[str, float | int] = {}
            if error_rate is not None:
                changes["error_rate"] = _clamp(error_rate, 0.0, 1.0)
            if latency_ms is not None:
                changes["latency_ms"] = int(_clamp(latency_ms, 0, 60_000))
            if latency_rate is not None:
                changes["latency_rate"] = _clamp(latency_rate, 0.0, 1.0)
            self._config = replace(self._config, **changes)
            return self._config

    def reset(self) -> FaultConfig:
        with self._lock:
            self._config = FaultConfig()
            return self._config

    @staticmethod
    def is_exempt(path: str) -> bool:
        return path.startswith(EXEMPT_PREFIXES)

    def should_error(self, path: str) -> bool:
        if self.is_exempt(path):
            return False
        rate = self._config.error_rate
        return rate > 0.0 and self._rng.random() < rate

    def latency_for(self, path: str) -> float:
        """Extra seconds to sleep before handling, 0.0 for none."""
        if self.is_exempt(path):
            return 0.0
        cfg = self._config
        if cfg.latency_ms <= 0:
            return 0.0
        if cfg.latency_rate < 1.0 and self._rng.random() >= cfg.latency_rate:
            return 0.0
        return cfg.latency_ms / 1000.0

    async def sleep_if_slow(self, path: str) -> float:
        delay = self.latency_for(path)
        if delay > 0.0:
            await asyncio.sleep(delay)
        return delay


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
