"""storefront - the edge service.

The only service exposed through the Ingress, so its SLIs are the ones a customer would
actually feel, and it is the service the canary rollout targets.

`GET /products/{id}` fans out to catalog and recs concurrently. That fan-out is what
makes the traces worth looking at, and it is why storefront's latency SLO (400ms) sits
above catalog's (200ms) but below recs' (800ms): the fan-out costs the slower of the two
legs, not their sum.
"""

from __future__ import annotations

import asyncio
import logging

import httpx
from fastapi import APIRouter, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from svc_common import DependencyClient, Settings, create_app, env_str

_log = logging.getLogger(__name__)

settings = Settings.from_env()
CATALOG_URL = env_str("CATALOG_URL", "http://catalog")
RECS_URL = env_str("RECS_URL", "http://recs")

router = APIRouter()


class ProductSummary(BaseModel):
    id: int
    name: str
    category: str
    price_cents: int


class ProductDetail(BaseModel):
    id: int
    name: str
    category: str
    price_cents: int
    stock: int
    recommendations: list[dict]
    recommendations_degraded: bool = Field(
        default=False,
        description="True when recs failed and the page was served without it.",
    )


class OrderRequest(BaseModel):
    product_id: int = Field(ge=1)
    quantity: int = Field(default=1, ge=1, le=10)


def _catalog(request: Request) -> DependencyClient:
    return request.app.state.catalog


def _recs(request: Request) -> DependencyClient:
    return request.app.state.recs


@router.get("/products", response_model=list[ProductSummary])
async def list_products(request: Request, limit: int = 20):
    try:
        products = await _catalog(request).get_json("/products", params={"limit": limit})
    except httpx.HTTPError as exc:
        # Same mapping as the other two handlers. Without this, a catalog failure on the
        # busiest endpoint in the traffic mix escapes as an unhandled 500 with a
        # traceback, while the identical failure on /products/{id} is a clean 503 - two
        # status codes for one incident, and a 4xx from catalog turned into a 5xx that
        # burns storefront's budget for a correct rejection.
        raise _upstream_error("catalog", exc) from None
    return [ProductSummary(**p) for p in products]


@router.get("/products/{product_id}", response_model=ProductDetail)
async def get_product(request: Request, product_id: int):
    product_task = _catalog(request).get_json(f"/products/{product_id}")
    recs_task = _recs(request).get_json(f"/recommendations/{product_id}")

    product, recs = await asyncio.gather(product_task, recs_task, return_exceptions=True)

    if isinstance(product, BaseException):
        raise _upstream_error("catalog", product)

    # recs is decorated content, not the product itself. Failing the whole page because
    # recommendations timed out would turn a degraded dependency into a full outage -
    # and would make the "slow dependency" scenario indistinguishable from a hard down.
    degraded = isinstance(recs, BaseException)
    if degraded:
        _log.warning("serving without recommendations",
                     extra={"product_id": product_id, "error": str(recs)})

    return ProductDetail(
        **product,
        recommendations=[] if degraded else recs["recommendations"],
        recommendations_degraded=degraded,
    )


@router.post("/orders", status_code=201)
async def create_order(request: Request, order: OrderRequest):
    try:
        return await _catalog(request).post_json("/orders", json=order.model_dump())
    except httpx.HTTPStatusError as exc:
        # Pass client errors through unchanged. Re-labelling catalog's 404/409 as a 500
        # here would burn storefront's error budget for a correct rejection.
        status = exc.response.status_code
        if 400 <= status < 500:
            raise HTTPException(status_code=status,
                                detail=_detail(exc.response)) from None
        raise _upstream_error("catalog", exc) from None
    except httpx.HTTPError as exc:
        raise _upstream_error("catalog", exc) from None


def _detail(response: httpx.Response) -> str:
    try:
        return response.json().get("detail", response.text)
    except ValueError:
        return response.text


def _upstream_error(dependency: str, exc: BaseException) -> HTTPException:
    if isinstance(exc, httpx.HTTPStatusError) and 400 <= exc.response.status_code < 500:
        return HTTPException(status_code=exc.response.status_code,
                             detail=_detail(exc.response))
    _log.error("upstream failure", extra={"dependency": dependency, "error": str(exc)})
    return HTTPException(status_code=503, detail=f"{dependency} unavailable")


async def _readiness() -> tuple[bool, dict[str, object]]:
    catalog_ok, recs_ok = await asyncio.gather(
        app.state.catalog.is_reachable(), app.state.recs.is_reachable()
    )
    # recs being down does not make storefront unready - the handler degrades around it.
    # Reporting NotReady here would remove every pod from the Service and turn a partial
    # degradation into a total outage.
    return catalog_ok, {
        "catalog": "up" if catalog_ok else "down",
        "recs": "up" if recs_ok else "down",
    }


async def _shutdown(fastapi_app: FastAPI) -> None:
    await asyncio.gather(
        fastapi_app.state.catalog.aclose(), fastapi_app.state.recs.aclose()
    )


app = create_app(
    settings=settings,
    readiness=_readiness,
    on_shutdown=_shutdown,
    title="storefront",
)
app.state.catalog = DependencyClient("catalog", CATALOG_URL, app.state.metrics)
app.state.recs = DependencyClient("recs", RECS_URL, app.state.metrics)
app.include_router(router)
