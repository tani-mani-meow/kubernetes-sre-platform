"""catalog - the database-backed service.

Owns products and orders. Its readiness probe is the only one in the platform that
depends on something external, which makes it the service where a Postgres pod kill is
actually visible: pods go NotReady, the Service removes them, storefront's dependency
metrics spike, and the availability SLI burns.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from svc_common import Metrics, Settings, create_app, env_int, env_str

from .store import (
    InMemoryStore,
    OutOfStock,
    PostgresStore,
    Product,
    ProductNotFound,
    Store,
)

_log = logging.getLogger(__name__)

settings = Settings.from_env()
DATABASE_URL = env_str("DATABASE_URL")
# Floor of 2, matching PostgresStore's min_size. asyncpg rejects a pool whose max is
# below its min with a ValueError at startup, so a value of 1 here would have been a
# crash rather than a small pool.
POOL_MAX = env_int("DB_POOL_MAX", 10, minimum=2, maximum=100)

router = APIRouter()


class ProductOut(BaseModel):
    id: int
    name: str
    category: str
    price_cents: int
    stock: int

    @classmethod
    def of(cls, product: Product) -> ProductOut:
        return cls(**vars(product))


class OrderIn(BaseModel):
    product_id: int = Field(ge=1)
    quantity: int = Field(default=1, ge=1, le=10)


class OrderOut(BaseModel):
    id: int
    product_id: int
    quantity: int
    total_cents: int


def _store(request: Request):
    return request.app.state.store


@router.get("/products", response_model=list[ProductOut])
async def list_products(request: Request, limit: int = 20, offset: int = 0):
    limit = max(1, min(limit, 100))
    products = await _store(request).list_products(limit, max(0, offset))
    return [ProductOut.of(p) for p in products]


@router.get("/products/{product_id}", response_model=ProductOut)
async def get_product(request: Request, product_id: int):
    try:
        return ProductOut.of(await _store(request).get_product(product_id))
    except ProductNotFound:
        raise HTTPException(status_code=404, detail="product not found") from None


@router.post("/orders", response_model=OrderOut, status_code=201)
async def create_order(request: Request, order: OrderIn):
    try:
        created = await _store(request).create_order(order.product_id, order.quantity)
    except ProductNotFound:
        raise HTTPException(status_code=404, detail="product not found") from None
    except OutOfStock:
        # 409, not 500: a client asking for more units than exist is a client error, and
        # counting it as a server error would burn the availability budget for a
        # correctly-working service.
        raise HTTPException(status_code=409, detail="insufficient stock") from None
    return OrderOut(
        id=created.id,
        product_id=created.product_id,
        quantity=created.quantity,
        total_cents=created.total_cents,
    )


def build_store(metrics: Metrics | None = None) -> Store:
    if DATABASE_URL:
        return PostgresStore(DATABASE_URL, max_size=POOL_MAX, metrics=metrics)
    _log.warning("DATABASE_URL not set, using in-memory store")
    return InMemoryStore()


async def _startup(app: FastAPI) -> None:
    await app.state.store.connect()


async def _shutdown(app: FastAPI) -> None:
    await app.state.store.close()


async def _readiness() -> tuple[bool, dict[str, object]]:
    ok = await app.state.store.ping()
    return ok, {"database": "up" if ok else "down"}


app = create_app(
    settings=settings,
    readiness=_readiness,
    on_startup=_startup,
    on_shutdown=_shutdown,
    title="catalog",
)
app.state.store = build_store(app.state.metrics)
app.include_router(router)
