"""FastAPI application factory.

Every service is `create_app(...)` plus its own routes. Keeping the platform wiring in
one place is what makes the three services comparable on a dashboard: identical metric
names, identical health semantics, identical fault behaviour.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware

from .config import Settings
from .faults import FaultConfig, FaultInjector
from .logging import configure_logging
from .logs import setup_log_export
from .metrics import Metrics, make_metrics_middleware
from .tracing import setup_tracing

_log = logging.getLogger(__name__)

ReadinessCheck = Callable[[], Awaitable[tuple[bool, dict[str, object]]]]


class FaultUpdate(BaseModel):
    """Runtime fault override. Every field optional - omitted fields keep their value."""

    error_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    latency_ms: int | None = Field(default=None, ge=0, le=60_000)
    latency_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    reset: bool = False


def create_app(
    *,
    settings: Settings | None = None,
    readiness: ReadinessCheck | None = None,
    on_startup: Callable[[FastAPI], Awaitable[None]] | None = None,
    on_shutdown: Callable[[FastAPI], Awaitable[None]] | None = None,
    title: str | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    configure_logging(settings.service_name, settings.log_level)

    metrics = Metrics(settings.service_name)
    faults = FaultInjector(FaultConfig.from_env())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        _log.info(
            "starting",
            extra={"version": settings.service_version, "faults": faults.config.as_dict()},
        )
        if on_startup:
            await on_startup(app)
        yield
        if on_shutdown:
            await on_shutdown(app)
        _log.info("stopped")

    app = FastAPI(
        title=title or settings.service_name,
        version=settings.service_version,
        lifespan=lifespan,
        # No interactive docs: this is a load-bearing demo surface, not a public API,
        # and /docs would be one more unmetered route to reason about.
        docs_url=None,
        redoc_url=None,
    )

    app.state.settings = settings
    app.state.metrics = metrics
    app.state.faults = faults

    app.include_router(_platform_router(settings, metrics, faults, readiness))

    # Middleware order: the last one added is the outermost. Faults go on first so the
    # metrics layer wraps them and therefore counts injected 500s and measures injected
    # latency - a fault the SLI cannot see is not a fault worth injecting.
    app.add_middleware(BaseHTTPMiddleware, dispatch=_make_fault_middleware(metrics, faults))
    app.add_middleware(BaseHTTPMiddleware, dispatch=make_metrics_middleware(metrics))

    # Tracing first: the log exporter's handler reads trace context off the active span,
    # so the tracer provider has to exist before any log record is exported.
    setup_tracing(app, settings)
    setup_log_export(settings)
    return app


def _make_fault_middleware(metrics: Metrics, faults: FaultInjector):
    async def middleware(request: Request, call_next):
        path = request.url.path

        delay = await faults.sleep_if_slow(path)
        if delay > 0.0:
            metrics.record_fault("latency")

        if faults.should_error(path):
            metrics.record_fault("error")
            _log.warning("injected fault", extra={"path": path, "kind": "error"})
            return JSONResponse(
                status_code=500,
                content={"detail": "injected fault", "service": metrics.service},
            )

        return await call_next(request)

    return middleware


def _platform_router(settings: Settings, metrics: Metrics, faults: FaultInjector,
                     readiness: ReadinessCheck | None) -> APIRouter:
    router = APIRouter()

    @router.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        # Liveness answers "is this process wedged", not "can it serve traffic".
        # Tying it to dependencies would restart healthy pods during a backend outage.
        return {"status": "ok", "service": settings.service_name}

    @router.get("/readyz", include_in_schema=False)
    async def readyz() -> JSONResponse:
        if readiness is None:
            return JSONResponse({"status": "ready", "service": settings.service_name})
        ok, detail = await readiness()
        return JSONResponse(
            status_code=200 if ok else 503,
            content={
                "status": "ready" if ok else "not_ready",
                "service": settings.service_name,
                **detail,
            },
        )

    @router.get("/metrics", include_in_schema=False)
    async def prometheus_metrics(request: Request):
        # Prometheus sends Accept: application/openmetrics-text when exemplar storage
        # is enabled, and exemplars only exist in that format.
        return metrics.render(request.headers.get("accept", ""))

    @router.get("/internal/faults", include_in_schema=False)
    async def get_faults() -> dict[str, object]:
        return {"service": settings.service_name, **faults.config.as_dict()}

    @router.put("/internal/faults", include_in_schema=False)
    async def put_faults(update: FaultUpdate) -> dict[str, object]:
        if update.reset:
            config = faults.reset()
        else:
            config = faults.update(
                error_rate=update.error_rate,
                latency_ms=update.latency_ms,
                latency_rate=update.latency_rate,
            )
        _log.warning("fault config changed", extra=dict(config.as_dict()))
        return {"service": settings.service_name, **config.as_dict()}

    return router
