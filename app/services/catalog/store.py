"""Catalog persistence.

Two implementations behind one small interface: asyncpg against Postgres in a cluster,
and an in-memory one so the unit tests and the CI overlay can run the real handlers
without a database. The CI e2e job does run against real Postgres - this exists so a
`pytest` run needs nothing but Python.
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from svc_common import Metrics

_log = logging.getLogger(__name__)

#: Dependency label used for the database in dependency_requests_total. The three
#: services report outbound calls under one metric family regardless of protocol, so
#: "catalog is slow because Postgres is slow" reads the same way on a dashboard as
#: "storefront is slow because recs is slow".
DATABASE = "postgres"


def _is_timeout(exc: BaseException) -> bool:
    """True for both flavours of "the database took too long"."""
    if isinstance(exc, TimeoutError):  # asyncio.TimeoutError, i.e. pool acquisition
        return True
    # Imported lazily so the in-memory store works without asyncpg installed.
    try:
        from asyncpg.exceptions import QueryCanceledError
    except ImportError:
        return False
    return isinstance(exc, QueryCanceledError)


class OutOfStock(Exception):
    """Raised when an order asks for more units than the product has."""


class ProductNotFound(Exception):
    pass


@dataclass(frozen=True)
class Product:
    id: int
    name: str
    category: str
    price_cents: int
    stock: int


@dataclass(frozen=True)
class Order:
    id: int
    product_id: int
    quantity: int
    total_cents: int
    created_at: datetime


class Store(Protocol):
    async def connect(self) -> None: ...
    async def close(self) -> None: ...
    async def ping(self) -> bool: ...
    async def list_products(self, limit: int, offset: int) -> list[Product]: ...
    async def get_product(self, product_id: int) -> Product: ...
    async def create_order(self, product_id: int, quantity: int) -> Order: ...


class PostgresStore:
    def __init__(self, dsn: str, min_size: int = 2, max_size: int = 10,
                 metrics: Metrics | None = None) -> None:
        self._dsn = dsn
        self._min_size = min_size
        self._max_size = max_size
        self._pool = None
        # Optional so the store can be constructed in a test without a registry.
        self._metrics = metrics

    @asynccontextmanager
    async def _measured(self, operation: str):
        """Time one database interaction and record its outcome.

        Without this the catalog dashboard can only show total handler latency, which
        cannot distinguish "the query got slower" from "the service got slower". During
        the Postgres pod-kill scenario that difference is the entire diagnosis.
        """
        started = time.perf_counter()
        outcome = "error"
        try:
            yield
            outcome = "ok"
        except (ProductNotFound, OutOfStock):
            # A product that does not exist is not a database failure. Counting it as
            # one would make the dependency error rate track client behaviour.
            outcome = "ok"
            raise
        except Exception as exc:
            # Two different timeouts, both meaning "the query ran long" rather than "the
            # database refused us", and that distinction is the whole diagnosis during a
            # Postgres incident. TimeoutError covers pool acquisition; asyncpg raises
            # QueryCanceledError when command_timeout expires, and that is a
            # PostgresError, so a bare `except TimeoutError` records it as a generic
            # error.
            outcome = "timeout" if _is_timeout(exc) else "error"
            raise
        finally:
            # One clock read, so the duration that gets logged is the same one recorded
            # on the histogram rather than a second reading a few microseconds later.
            elapsed = time.perf_counter() - started
            if self._metrics is not None:
                self._metrics.observe_dependency(DATABASE, outcome, elapsed)
            if elapsed > 1.0:
                _log.warning("slow query", extra={"operation": operation,
                                                  "seconds": round(elapsed, 3)})

    async def connect(self) -> None:
        """Open the pool. Never raises.

        A failure here used to propagate out of the FastAPI lifespan, which made uvicorn
        exit and the Deployment restart the pod - so any catalog pod that rolled during a
        Postgres outage went into CrashLoopBackOff. That contradicts the pod-kill-postgres
        hypothesis ("catalog should go NotReady rather than crash-loop") and makes
        recovery worse, because a pod in backoff is not waiting to serve, it is waiting
        out an exponential timer.

        Staying up with no pool gives exactly the intended behaviour: ping() returns
        False, /readyz reports 503, the Service drops the pod, and it rejoins on its own
        once the database is back.
        """
        try:
            await self._open_pool()
        except Exception as exc:
            _log.warning("could not connect to postgres at startup; will retry on demand",
                         extra={"error": str(exc)})

    async def _open_pool(self) -> None:
        import asyncpg

        self._pool = await asyncpg.create_pool(
            self._dsn,
            min_size=self._min_size,
            max_size=self._max_size,
            # Bounded so a stalled database surfaces as a fast 5xx the SLI can see,
            # instead of upstream requests piling up until storefront's pool is empty.
            command_timeout=2.0,
            timeout=3.0,
        )
        _log.info("connected to postgres", extra={"pool_max": self._max_size})

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def ping(self) -> bool:
        # The readiness probe is also the reconnect trigger: if startup could not open
        # the pool, the next probe retries. That is what lets a pod that came up during
        # a database outage rejoin the Service on its own.
        if self._pool is None:
            try:
                await self._open_pool()
            except Exception as exc:
                _log.debug("postgres still unreachable", extra={"error": str(exc)})
                return False

        # Measured like any other database call. Readiness probes are the only traffic
        # catalog sends to Postgres once it has gone NotReady and stopped receiving
        # requests - without this, dependency_requests_total{dependency="postgres"} goes
        # to zero during exactly the outage it is supposed to describe, and
        # DependencyHardDown's `rate(...) > 0` conjunct turns the alert off.
        try:
            async with self._measured("ping"), self._pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            return True
        except Exception as exc:  # asyncpg raises a wide family of connection errors
            _log.warning("postgres ping failed", extra={"error": str(exc)})
            return False

    async def list_products(self, limit: int, offset: int) -> list[Product]:
        async with self._measured("list_products"):
            rows = await self._pool.fetch(
                """
                SELECT id, name, category, price_cents, stock
                FROM products
                ORDER BY id
                LIMIT $1 OFFSET $2
                """,
                limit,
                offset,
            )
        return [Product(**dict(row)) for row in rows]

    async def get_product(self, product_id: int) -> Product:
        async with self._measured("get_product"):
            row = await self._pool.fetchrow(
                """
                SELECT id, name, category, price_cents, stock
                FROM products WHERE id = $1
                """,
                product_id,
            )
            if row is None:
                raise ProductNotFound(product_id)
        return Product(**dict(row))

    async def create_order(self, product_id: int, quantity: int) -> Order:
        async with self._measured("create_order"), \
                   self._pool.acquire() as conn, conn.transaction():
            # FOR UPDATE, not a read-then-write: two concurrent orders for the last
            # unit must not both succeed. The load generator does run concurrent
            # POST /orders, so this is reachable, not theoretical.
            product = await conn.fetchrow(
                "SELECT id, price_cents, stock FROM products WHERE id = $1 FOR UPDATE",
                product_id,
            )
            if product is None:
                raise ProductNotFound(product_id)
            if product["stock"] < quantity:
                raise OutOfStock(product_id)

            await conn.execute(
                "UPDATE products SET stock = stock - $1 WHERE id = $2",
                quantity,
                product_id,
            )
            row = await conn.fetchrow(
                """
                    INSERT INTO orders (product_id, quantity, total_cents)
                    VALUES ($1, $2, $3)
                    RETURNING id, product_id, quantity, total_cents, created_at
                    """,
                product_id,
                quantity,
                product["price_cents"] * quantity,
            )
            return Order(**dict(row))


class InMemoryStore:
    """Test double. Same semantics, no database."""

    def __init__(self, products: list[Product] | None = None) -> None:
        self._products = {p.id: p for p in (products or _sample_products())}
        self._orders: list[Order] = []
        self.healthy = True

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def ping(self) -> bool:
        return self.healthy

    async def list_products(self, limit: int, offset: int) -> list[Product]:
        ordered = [self._products[k] for k in sorted(self._products)]
        return ordered[offset:offset + limit]

    async def get_product(self, product_id: int) -> Product:
        try:
            return self._products[product_id]
        except KeyError:
            raise ProductNotFound(product_id) from None

    async def create_order(self, product_id: int, quantity: int) -> Order:
        product = await self.get_product(product_id)
        if product.stock < quantity:
            raise OutOfStock(product_id)
        self._products[product_id] = replace(product, stock=product.stock - quantity)
        order = Order(
            id=len(self._orders) + 1,
            product_id=product_id,
            quantity=quantity,
            total_cents=product.price_cents * quantity,
            created_at=datetime.now(tz=UTC),
        )
        self._orders.append(order)
        return order


def _sample_products() -> list[Product]:
    """Mirrors the first rows of migrations/002_seed.sql so tests and the cluster agree."""
    return [
        Product(1, "Mechanical Keyboard", "peripherals", 12900, 40),
        Product(2, "27-inch Monitor", "displays", 31900, 15),
        Product(3, "USB-C Dock", "peripherals", 18900, 25),
    ]
