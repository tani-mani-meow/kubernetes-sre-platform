"""Test environment.

Set here rather than inside each test module because the services read their config at
import time - a module-level `os.environ` poke before the imports would work, but it
forces every test file into an import-ordering dance. conftest.py runs first by design.
"""

import os

os.environ.setdefault("SERVICE_NAME", "test")
os.environ.setdefault("SERVICE_VERSION", "test")
os.environ.setdefault("LOG_LEVEL", "WARNING")

# No collector in a unit test run, and no network calls to a nonexistent one.
os.environ["OTEL_SDK_DISABLED"] = "true"

# catalog falls back to its in-memory store when DATABASE_URL is empty.
os.environ.pop("DATABASE_URL", None)

# recs: keep the suite fast. The shape under test is base/tail/jitter, not the
# wall-clock values a cluster runs with.
os.environ.setdefault("RECS_BASE_LATENCY_MS", "10")
os.environ.setdefault("RECS_TAIL_LATENCY_MS", "40")
