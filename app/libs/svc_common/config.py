"""Environment-derived settings shared by every service.

Parsing is lenient by design: a malformed FAULT_ERROR_RATE should not crash a pod at
3am, it should fall back to "no fault" and say so in the logs.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

_log = logging.getLogger(__name__)


def env_str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def env_float(name: str, default: float, minimum: float | None = None,
              maximum: float | None = None) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        _log.warning("invalid float in %s=%r, using %s", name, raw, default)
        return default
    if minimum is not None and value < minimum:
        _log.warning("%s=%s below minimum %s, clamping", name, value, minimum)
        value = minimum
    if maximum is not None and value > maximum:
        _log.warning("%s=%s above maximum %s, clamping", name, value, maximum)
        value = maximum
    return value


def env_int(name: str, default: int, minimum: int | None = None,
            maximum: int | None = None) -> int:
    low = None if minimum is None else float(minimum)
    high = None if maximum is None else float(maximum)
    return int(env_float(name, float(default), low, high))


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    service_name: str
    service_version: str
    log_level: str
    otel_enabled: bool
    otel_endpoint: str

    @classmethod
    def from_env(cls) -> Settings:
        name = env_str("SERVICE_NAME")
        if not name:
            raise RuntimeError("SERVICE_NAME is required")
        return cls(
            service_name=name,
            service_version=env_str("SERVICE_VERSION", "dev"),
            log_level=env_str("LOG_LEVEL", "INFO").upper(),
            # Mirrors the OTel spec's own kill switch so the SDK and our wiring agree.
            otel_enabled=not env_bool("OTEL_SDK_DISABLED", False),
            otel_endpoint=env_str("OTEL_EXPORTER_OTLP_ENDPOINT"),
        )
