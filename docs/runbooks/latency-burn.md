# Runbook: `*LatencySLOBurn`

Covers `StorefrontLatencySLOBurn`, `CatalogLatencySLOBurn` and `RecsLatencySLOBurn`. Read
the `service` and `severity` labels first — both severities share one `alertname`.

## What this means

The SLI is not a percentile. It is the fraction of requests that took longer than a fixed
threshold, counted exactly off a histogram bucket boundary:

```promql
1 - (
  sum(rate(http_request_duration_seconds_bucket{service="catalog", le="0.2"}[5m]))
  /
  sum(rate(http_request_duration_seconds_count{service="catalog"}[5m]))
)
```

The objective is 99% for all three services, so the error budget is 1% of requests and the
14.4x page threshold is `14.4 × 0.01 = 0.144`.

| Service | Threshold | Healthy slow-fraction | Resting burn rate | Page fires at (14.4x) |
|---|---|---|---|---|
| `storefront` | 400ms | ~0.3% | ~0.30 | 14.4% of requests over 400ms |
| `catalog` | 200ms | ~0% | ~0 | 14.4% over 200ms |
| `recs` | 800ms | ~0% | ~0 | 14.4% over 800ms |

Storefront is the only one with a non-zero resting figure, and it is not a problem: 15% of
load-generator traffic hits the fan-out route and 2% of recs calls take their designed
600–700ms tail, so `0.15 × 0.02 = 0.3%` of storefront requests exceed 400ms with nothing
wrong. Against a 1% budget that is a burn rate of 0.30. Anything materially above that on
storefront is real.

These objectives were 95% until the arithmetic was checked; at a 5% budget the page
threshold worked out to 72% of requests being slow. If you find a dashboard, panel
description or older note still quoting 72%, it predates the change — the live number is in
`gitops/slo/generated/*-rules.yaml` and the derivation is in `docs/slo-methodology.md`.

## Page or ticket

**`severity: page`** — 14.4x over 1h+5m (14.4% slow), or 6x over 6h+30m (6% slow).

**`severity: ticket`** — 3x over 24h+2h (3% slow), or 1x over 72h+6h (1% slow). The 1x tier
is worth reading literally: it fires when the service has been sitting exactly at its
objective for three days. That is a capacity or design problem, not an incident.

Both windows in a tier must be true simultaneously, so the alert fires when the long window
crosses — roughly `60 min × 0.144 / e` for an observed slow-fraction `e`. At `e = 1.0`
(everything over the line) that is 8m 38s. A latency blip lasting five minutes moves the 5m
window and never the 1h one, so if you have been paged, the degradation has been running for
at least nine minutes.

## First five minutes

```bash
bash scripts/urls.sh
```

**Is this a chaos run?** Two mechanisms inject latency here and they look different.

```promql
sum by (service, kind) (rate(fault_injected_total{kind="latency"}[5m]))
```

Non-zero means application-level injection (`FAULT_LATENCY_MS` via `PUT /internal/faults`).
Zero does **not** rule out chaos: `chaos/experiments/latency-catalog.yaml` is a Chaos Mesh
`NetworkChaos`, which is below the application and produces no `fault_injected_total` at
all. Check for it directly:

```bash
kubectl -n demo get networkchaos,podchaos
ls chaos/harness/out/
```

**Which service, and which route?** The route breakdown is what separates the fan-out path
from the cheap read:

```promql
1 - (
  sum by (route) (rate(http_request_duration_seconds_bucket{service="storefront", le="0.4"}[5m]))
  /
  sum by (route) (rate(http_request_duration_seconds_count{service="storefront"}[5m]))
)
```

`/products` touches catalog only. `/products/{product_id}` fans out to catalog and recs
concurrently and costs the slower leg. `/orders` is a catalog write. If only
`/products/{product_id}` is slow, look at recs; if all three are slow, look at catalog or
Postgres.

**Where does the burn stand?**

```promql
slo:current_burn_rate:ratio{sloth_slo="latency"}
```

**Where is the time actually going?** This is the query that localises the fault, because
it compares the caller's view of a dependency against the dependency's own view of itself:

```promql
# storefront's measurement of its outbound calls
1 - (
  sum by (dependency) (rate(dependency_request_duration_seconds_bucket{service="storefront", le="0.4"}[5m]))
  /
  sum by (dependency) (rate(dependency_request_duration_seconds_count{service="storefront"}[5m]))
)

# catalog's measurement of its own request handling
1 - (
  sum(rate(http_request_duration_seconds_bucket{service="catalog", le="0.2"}[5m]))
  /
  sum(rate(http_request_duration_seconds_count{service="catalog"}[5m]))
)
```

If storefront sees catalog as slow but catalog sees itself as fast, the latency is
**between** them — network, DNS, or endpoint churn — not inside catalog. That gap is the
entire signature of the `latency-catalog` scenario, and it is invisible in catalog's own
logs and traces by design.

**Dashboards.** Grafana at `http://localhost:3000`. The per-service dashboards
(`storefront — service SLO` and its catalog/recs siblings, uids `slo-storefront`,
`slo-catalog`, `slo-recs`) carry the latency panels; `SLO overview — error budgets`
(uid `slo-overview`) carries the burn rates.

## Telling this system's failure modes apart

### The recs tail is the design, not a fault

`recs` sleeps `RECS_BASE_LATENCY_MS` (250ms) with ±20% jitter and adds
`RECS_TAIL_LATENCY_MS` (400ms) to `RECS_TAIL_RATE` (2%) of calls. The designed tail lands
at 600–700ms, under the 800ms threshold, so recs' healthy slow-fraction is near zero.

That tail rate is coupled to storefront's latency budget, not just to recs': it reaches
storefront through the 15% fan-out share, and `0.15 × RECS_TAIL_RATE` is storefront's
resting breach rate. At 0.05 it would be 0.75% against a 1% budget — a 0.75 burn rate with
nothing broken. If you find storefront's latency burn sitting near 0.75 rather than 0.30,
check this value before looking anywhere else.

**Any sustained non-zero slow-fraction on recs means something changed.** Check the
config the pods are actually using before suspecting the platform. The ConfigMap name
carries a content hash, so read it off the pod rather than guessing — a pod still
referencing an older hash is itself the answer:

```bash
cm=$(kubectl -n demo get pod -l app.kubernetes.io/name=recs \
  -o jsonpath='{.items[0].spec.containers[0].envFrom[0].configMapRef.name}')
kubectl -n demo get cm "$cm" -o jsonpath='{.data}'; echo
```

Expected: `RECS_BASE_LATENCY_MS=250`, `RECS_TAIL_LATENCY_MS=400`, `RECS_TAIL_RATE=0.02`.

Then node CPU. `recs` spends its time in `asyncio.sleep`, so it should be almost free; a
recs slowdown that is not a config change is usually the node, not the service.

### catalog slow (everything downstream inherits it)

catalog's threshold is the tightest at 200ms, and storefront's fan-out cannot be faster than
its slowest leg, so a catalog latency burn drags storefront's with it. That pair — catalog
burning, storefront burning, recs flat — is the localisation signal the dashboards exist to
produce.

First, separate a slow query from a slow service. catalog records its database calls in the
same dependency metric family storefront uses for its HTTP calls:

```promql
histogram_quantile(0.95,
  sum by (le) (rate(dependency_request_duration_seconds_bucket{service="catalog", dependency="postgres"}[5m]))
)

sum by (outcome) (rate(dependency_requests_total{service="catalog", dependency="postgres"}[5m]))
```

If the database timings are flat and catalog's handler latency is not, the time is going
somewhere inside catalog. If they move together, it is Postgres.

Candidates, in the order worth checking:

```bash
# Pool exhaustion. DB_POOL_MAX defaults to 10 and is not set in the base ConfigMap,
# so an empty result here means the default is in force.
cm=$(kubectl -n demo get pod -l app.kubernetes.io/name=catalog \
  -o jsonpath='{.items[0].spec.containers[0].envFrom[0].configMapRef.name}')
kubectl -n demo get cm "$cm" -o jsonpath='{.data}'; echo
kubectl -n demo logs -l app.kubernetes.io/name=catalog --tail=200 | grep -i pool

# Postgres itself: checkpoint, autovacuum, or a restart.
kubectl -n demo get pods -l app.kubernetes.io/name=postgres
kubectl -n demo logs postgres-0 --tail=100
```

**CPU throttling is not a candidate.** No container in this platform sets a CPU limit —
deliberately, because CFS throttling surfaces as request latency and would turn every
latency SLO into a measurement of the limit rather than of the service. Verify if you doubt
it:

```bash
kubectl -n demo get pods -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.containers[*].resources.limits}{"\n"}{end}'
```

Node-level CPU contention is still possible, especially on kind where every node is a
container on one laptop. Check node pressure rather than pod throttling.

### Postgres gone rather than slow

If catalog latency has spiked and then catalog stopped serving altogether, this is the
availability failure, not a latency one. `/readyz` pings the pool and takes every catalog
replica out of the Service within about ten seconds. See
[`availability-burn.md`](availability-burn.md).

### A canary in progress

```bash
kubectl argo rollouts get rollout storefront -n demo
```

```promql
1 - (
  sum by (rollouts_pod_template_hash) (rate(http_request_duration_seconds_bucket{service="storefront", le="0.4"}[2m]))
  /
  sum by (rollouts_pod_template_hash) (rate(http_request_duration_seconds_count{service="storefront"}[2m]))
)
```

A freshly started pod pays cold-start costs — connection pools, first-request imports — that
are not a defect, so the AnalysisTemplate's latency gate is a loose `> 0.10`: a canary under
10% slow is inside its gate and will be promoted.

The two numbers to hold side by side are 10% (the deploy gate, measured over `[2m]` on the
canary pods alone) and 14.4% (the page threshold, measured over 1h across the whole
Service). The gate is the tighter of the two and it acts in about two minutes, so a canary
bad enough to page should always abort first. If it is confined to one hash and above 10%
and has not aborted, the analysis is not running — check that the `AnalysisRun` exists and
that `rollouts_pod_template_hash` is present on the series.

### A node drain in progress

```bash
kubectl get nodes                  # SchedulingDisabled
kubectl -n demo get pods -o wide   # pods moving between nodes
```

A latency bump during a drain is expected and is what the `node-drain` scenario predicts —
rescheduled pods are cold, and on kind the surviving worker is carrying twice the load.
`whenUnsatisfiable: ScheduleAnyway` on the topology spread constraints means pods land
somewhere rather than going Pending, which is why this shows as latency and not as errors.
It should not page. If it does, the finding is that the remaining node cannot carry the
load, which is worth recording.

## Rolling back

Same rules as the availability runbook: roll back only when the slowness is attributable to
a revision.

```bash
kubectl argo rollouts abort storefront -n demo    # traffic back to stable; status only
kubectl argo rollouts undo storefront -n demo     # spec change — selfHeal will revert it
```

`undo` changes the pod template and the storefront Application has `selfHeal: true`, so
Argo CD restores the bad template within seconds. Use `abort` to stop the bleeding and a Git
revert to make it stick. `make rollout-retry` restarts an aborted rollout once the fix has
landed.

## What not to do

**Do not restart pods to make latency go away.** A restart adds cold pools and cold caches
to a service that is already slow, and every new catalog pod waits on its
`wait-for-postgres` init container first. If the cause is a dependency, restarting makes the
number worse and destroys the fault config any chaos run is holding.

**Do not add a CPU limit "to make it more predictable".** It is the one change guaranteed to
make a latency SLO measure something other than the service. The absence of CPU limits
across this platform is a decision, documented in the manifests.

**Do not raise the SLO threshold to clear the alert.** The thresholds are histogram bucket
boundaries (`LATENCY_BUCKETS` in `svc_common.metrics`), so changing one means adding a
bucket and losing comparability with every observation recorded before the change. If a
threshold is genuinely wrong, change it in `gitops/slo/specs/`, run `make slo-gen`, and
commit the regenerated rules — and note in `planning.md` that the historical series is not
comparable across the edit.

**Do not use `histogram_quantile` to decide whether the SLO is met.** It interpolates inside
whichever bucket holds the percentile and will disagree with the alert. Use the bucket ratio
the alert uses. `histogram_quantile` is fine for a dashboard trend line and wrong for a
verdict.

**Do not call it recovered when the injected latency stops.** The 5m rate window trails the
system by minutes. Recovery is `slo:current_burn_rate:ratio` back under 1.0 and holding for
180 seconds.

## After

Write it up with `evidence/templates/incident-report.md` if it was real. If it was a
scenario, `chaos/harness/run.py` has already written the report and `make evidence` rebuilds
the summary table.
