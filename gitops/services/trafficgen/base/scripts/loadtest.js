// Background load for the SLO demo.
//
// The SLIs are computed from the services' own Prometheus metrics, so this script's job
// is only to keep a steady, realistic request stream flowing through storefront. It
// deliberately does nothing a browser would not do: no special headers, no bypass paths,
// no direct calls to catalog or recs. If these requests were distinguishable from real
// traffic, the error budget would be measuring the load generator rather than the
// service.

import http from 'k6/http';

const BASE = __ENV.STOREFRONT_URL || 'http://storefront';
const RPS = Number(__ENV.TARGET_RPS || 8);

// Matches the twelve rows in 002_seed.sql. Requesting ids outside that range would give
// storefront a stream of legitimate 404s, which are 4xx and therefore invisible to an
// availability SLI built on 5xx - so it would not break the numbers, but it would make
// every trace exemplar in Tempo a not-found.
const PRODUCT_COUNT = 12;

export const options = {
  // constant-arrival-rate, not constant-VUs: offered load must stay flat when the system
  // slows down. Under constant-VUs a latency injection would *reduce* the request rate,
  // the SLI denominator would shrink exactly when the numerator grows, and the burn-rate
  // alert would fire late and recover early.
  scenarios: {
    browse: {
      executor: 'constant-arrival-rate',
      rate: RPS,
      timeUnit: '1s',
      // 30 days. k6 requires a bounded duration; the Deployment restarts the container
      // when it ends. See the Deployment for why this is not a `while true` wrapper.
      duration: '720h',
      // ~8 RPS at a normal ~300ms response needs about 3 VUs. maxVUs 100 leaves room for
      // responses degrading to ~12s before k6 starts reporting dropped_iterations, which
      // would quietly reduce offered load during exactly the incidents being measured.
      preAllocatedVUs: 10,
      maxVUs: 100,
    },
  },
  // No thresholds, on purpose. A breached threshold makes k6 exit non-zero, the container
  // restarts, and the traffic gap lands in the middle of whatever failure was being
  // measured. Pass/fail lives in Prometheus and the burn-rate alerts, not here.
  thresholds: {},
  // Nothing inspects response bodies, and holding them costs memory on a kind node.
  discardResponseBodies: true,
};

export default function () {
  const roll = Math.random();

  if (roll < 0.8) {
    // The cheap read: catalog only, no fan-out. Dominating the mix keeps storefront's
    // aggregate p95 near catalog's latency, so the 400ms SLO has real headroom and a
    // regression in the fan-out path is what consumes it.
    http.get(`${BASE}/products`, { tags: { name: 'GET /products' } });
    return;
  }

  const id = 1 + Math.floor(Math.random() * PRODUCT_COUNT);

  if (roll < 0.95) {
    // The interesting one: storefront fans out to catalog and recs concurrently. This is
    // the path that produces the two-legged traces and the only path that exercises the
    // degrade-without-recs branch.
    http.get(`${BASE}/products/${id}`, { tags: { name: 'GET /products/{id}' } });
    return;
  }

  // Writes, at 5%: enough to keep the orders table growing and to exercise the SELECT
  // ... FOR UPDATE path in catalog, low enough that stock (1,000,000 per product) never
  // runs out and starts returning 409s that would look like a broken service.
  http.post(
    `${BASE}/orders`,
    JSON.stringify({ product_id: id, quantity: 1 }),
    {
      headers: { 'Content-Type': 'application/json' },
      tags: { name: 'POST /orders' },
    },
  );
}

// Note on tags: k6 tags are local to k6's own output and are never sent on the wire. The
// services label their metrics from the FastAPI route template, so these names exist only
// to keep k6's logs readable if someone runs the script by hand.
