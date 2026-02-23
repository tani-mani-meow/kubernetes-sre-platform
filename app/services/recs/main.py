"""recs - the deliberately slow dependency.

Stands in for a third-party recommendations API: every call sleeps for a baseline
period, and a small fraction of calls sleep considerably longer. That long tail is the
point. A service whose latency is uniformly 250ms makes for a boring dashboard; one
with a p50 near baseline and a p99 several times higher is what real dependencies look
like, and it is what makes a p95-based latency SLO worth having.

The tail is why `recs` gets the loosest thresholds in the platform (99% availability,
99% of requests under 800ms - the widest latency threshold of the three) - see
docs/conventions.md.
"""

from __future__ import annotations

import asyncio
import hashlib
import random

from fastapi import APIRouter
from pydantic import BaseModel
from svc_common import Settings, create_app, env_float, env_int

settings = Settings.from_env()

BASE_LATENCY_MS = env_int("RECS_BASE_LATENCY_MS", 250, minimum=0, maximum=10_000)
TAIL_LATENCY_MS = env_int("RECS_TAIL_LATENCY_MS", 400, minimum=0, maximum=10_000)
# 2%, not 5%. The tail reaches storefront through the 15% of traffic that fans out, and
# every tail request costs storefront a breach of its 400ms latency SLO. At a 5% tail
# that is 0.75% of storefront requests against a 1% budget - a service sitting at a 0.75
# burn rate while nothing is wrong, which leaves no room to detect anything that is.
TAIL_RATE = env_float("RECS_TAIL_RATE", 0.02, minimum=0.0, maximum=1.0)

#: Jitter as a fraction of the baseline. Without it every request lands in the same
#: histogram bucket and the latency panels look synthetic.
JITTER_FRACTION = 0.2

_rng = random.Random()

router = APIRouter()


class Recommendation(BaseModel):
    product_id: int
    score: float
    reason: str


class RecommendationsResponse(BaseModel):
    product_id: int
    recommendations: list[Recommendation]
    latency_ms: int
    slow_path: bool


_REASONS = (
    "frequently bought together",
    "similar customers viewed",
    "trending in this category",
    "completes the set",
)


def _simulated_work_seconds() -> tuple[float, bool]:
    """How long this call 'takes', and whether it hit the long tail."""
    jitter = 1.0 + _rng.uniform(-JITTER_FRACTION, JITTER_FRACTION)
    millis = BASE_LATENCY_MS * jitter
    slow_path = TAIL_RATE > 0.0 and _rng.random() < TAIL_RATE
    if slow_path:
        millis += TAIL_LATENCY_MS
    return max(0.0, millis / 1000.0), slow_path


def _recommendations_for(product_id: int, count: int = 3) -> list[Recommendation]:
    """Deterministic per product id, so responses are stable across replicas.

    Stable output matters during a canary: if stable and canary pods returned different
    recommendations for the same product, a reviewer could not tell an analysis failure
    from ordinary response variation.
    """
    seed = int(hashlib.sha256(str(product_id).encode()).hexdigest()[:8], 16)
    rng = random.Random(seed)
    candidates = [pid for pid in range(1, 13) if pid != product_id]
    picks = rng.sample(candidates, min(count, len(candidates)))
    return [
        Recommendation(
            product_id=pid,
            score=round(0.55 + rng.random() * 0.44, 3),
            reason=rng.choice(_REASONS),
        )
        for pid in picks
    ]


@router.get("/recommendations/{product_id}", response_model=RecommendationsResponse)
async def recommendations(product_id: int, limit: int = 3) -> RecommendationsResponse:
    delay, slow_path = _simulated_work_seconds()
    await asyncio.sleep(delay)
    return RecommendationsResponse(
        product_id=product_id,
        recommendations=_recommendations_for(product_id, max(1, min(limit, 10))),
        latency_ms=int(delay * 1000),
        slow_path=slow_path,
    )


app = create_app(settings=settings, title="recs")
app.include_router(router)
