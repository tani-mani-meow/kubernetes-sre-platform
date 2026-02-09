"""Outbound HTTP client for service-to-service calls.

Wraps httpx so every dependency call is measured the same way. The
dependency_requests_total / dependency_request_duration_seconds pair is what makes
"storefront is slow because recs is slow" visible on a dashboard without opening a
single trace - the trace then confirms it for one specific request.
"""

from __future__ import annotations

import logging
import time

import httpx

from .metrics import Metrics

_log = logging.getLogger(__name__)

#: Bounded well below the point where a slow backend becomes a stuck one. The read
#: timeout is 2s rather than something under storefront's 400ms SLO because recs is
#: *designed* to take ~250ms with a tail past 650ms - a timeout tighter than the
#: dependency's normal behaviour would turn every tail request into a failure. The
#: budget this protects is availability, not latency: a request that has already blown
#: the latency SLO should still return an answer rather than pile up a worker.
DEFAULT_TIMEOUT = httpx.Timeout(connect=1.0, read=2.0, write=2.0, pool=2.0)


class DependencyClient:
    """An httpx.AsyncClient bound to one named dependency, with metrics."""

    def __init__(self, name: str, base_url: str, metrics: Metrics,
                 timeout: httpx.Timeout | None = None,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self._metrics = metrics
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout or DEFAULT_TIMEOUT,
            # Keep-alive matters here: at the load generator's steady rate, a fresh TCP
            # handshake per call would show up as latency the services are not causing.
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=32),
            # Tests inject httpx.MockTransport so dependency failures can be exercised
            # against the real client, metrics recording included.
            transport=transport,
        )

    async def get_json(self, path: str, **kwargs) -> object:
        return await self._request_json("GET", path, **kwargs)

    async def post_json(self, path: str, json: object | None = None, **kwargs) -> object:
        return await self._request_json("POST", path, json=json, **kwargs)

    async def _request_json(self, method: str, path: str, **kwargs) -> object:
        started = time.perf_counter()
        outcome = "error"
        try:
            response = await self._client.request(method, path, **kwargs)
            if not response.is_success:
                outcome = f"http_{response.status_code // 100}xx"
                response.raise_for_status()
            payload = response.json()
            # Set only once the body has actually parsed. Marking the call "ok" on the
            # status line would record a healthy dependency for a 200 carrying an
            # ingress error page or a truncated body - json.JSONDecodeError is a
            # ValueError, not an httpx.HTTPError, so no except clause below would catch
            # it and the caller's 500 would be attributed to nothing at all.
            outcome = "ok"
            return payload
        except httpx.TimeoutException:
            outcome = "timeout"
            raise
        except httpx.HTTPStatusError:
            # Not dead code: HTTPStatusError subclasses HTTPError, so without this the
            # clause below would overwrite the http_4xx/http_5xx outcome set above with
            # "transport_error" and lose which status the dependency actually returned.
            raise
        except httpx.HTTPError:
            outcome = "transport_error"
            raise
        except ValueError:
            outcome = "bad_body"
            raise
        finally:
            self._metrics.observe_dependency(
                self.name, outcome, time.perf_counter() - started
            )

    async def is_reachable(self) -> bool:
        """Readiness probing for a dependency. Never raises."""
        try:
            response = await self._client.get("/healthz", timeout=httpx.Timeout(1.0))
            return response.is_success
        except httpx.HTTPError:
            return False

    async def aclose(self) -> None:
        await self._client.aclose()
