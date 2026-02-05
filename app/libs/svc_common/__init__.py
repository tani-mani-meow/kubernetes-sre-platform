"""Shared platform wiring for the demo services.

Config, fault injection, metrics, JSON logging, tracing and an instrumented HTTP client.
The services themselves are then small enough to read in one sitting, which is the point:
this repo is about the operations layer, not the application.
"""

from .app import FaultUpdate, create_app
from .config import Settings, env_bool, env_float, env_int, env_str
from .faults import FaultConfig, FaultInjector
from .http import DependencyClient
from .logging import configure_logging
from .metrics import LATENCY_BUCKETS, Metrics

__all__ = [
    "LATENCY_BUCKETS",
    "DependencyClient",
    "FaultConfig",
    "FaultInjector",
    "FaultUpdate",
    "Metrics",
    "Settings",
    "configure_logging",
    "create_app",
    "env_bool",
    "env_float",
    "env_int",
    "env_str",
]
