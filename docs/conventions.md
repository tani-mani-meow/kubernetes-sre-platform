# Conventions

The contract every component agrees on. Change here first, then propagate — a mismatch
between a metric name here and a Sloth spec is the kind of bug that silently produces an
SLO dashboard full of `No data`.

## Naming

| Thing | Value |
|---|---|
| Project / part-of label | `k8s-slo-platform` |
| App namespace | `demo` |
| Observability namespace | `monitoring` |
| Other namespaces | `argocd`, `argo-rollouts`, `chaos-mesh`, `ingress-nginx` |
| Services | `storefront`, `catalog`, `recs` |
| Container registry | `ghcr.io/<owner>/k8s-slo-platform/<service>` |
| Local image tag | `local` (with `imagePullPolicy: IfNotPresent`) |
| CI/EKS image tag | `sha-<short-git-sha>` |

## Kubernetes labels

Every workload carries:

```yaml
app.kubernetes.io/name: <service>            # storefront | catalog | recs | postgres | trafficgen
app.kubernetes.io/part-of: k8s-slo-platform
app.kubernetes.io/component: <api|database|loadgen>
```

Selectors use `app.kubernetes.io/name` only. `part-of` and `component` are for humans and
dashboards; adding them to a selector would make them immutable forever.

## Ports

| Component | Port | Notes |
|---|---|---|
| storefront / catalog / recs | `8000` | HTTP + `/metrics` on the same port, port name `http` |
| postgres | `5432` | |
| otel-collector | `4317` / `4318` | OTLP gRPC / HTTP |

Metrics share the application port deliberately: one port means one `PodMonitor` port name
and no second container listener to keep alive.

## HTTP endpoints (all services)

| Path | Purpose |
|---|---|
| `GET /healthz` | Liveness. Always 200 if the process is up. Never fault-injected. |
| `GET /readyz` | Readiness. Checks dependencies (DB for catalog). Never fault-injected. |
| `GET /metrics` | Prometheus exposition. Never fault-injected. |
| `GET /internal/faults` | Current fault config. Cluster-internal, never exposed via Ingress. |
| `PUT /internal/faults` | Runtime fault override, used by the chaos harness. |

Health and metrics paths bypass fault injection on purpose: a fault that made pods fail
their probes would trigger a restart, and Kubernetes healing the pod would mask the very
SLO burn the experiment is trying to measure.

## Metrics

Emitted by `svc_common.metrics` (hand-written, not an instrumentation library — full
control over label cardinality, and one less dependency to justify).

| Metric | Type | Labels |
|---|---|---|
| `http_requests_total` | counter | `service`, `method`, `route`, `status_code` |
| `http_request_duration_seconds` | histogram | `service`, `method`, `route` |
| `http_requests_in_flight` | gauge | `service` |
| `dependency_requests_total` | counter | `service`, `dependency`, `outcome` |
| `dependency_request_duration_seconds` | histogram | `service`, `dependency` |
| `fault_injected_total` | counter | `service`, `kind` (`error` \| `latency`) |

`route` is the **route template** (`/products/{product_id}`), never the raw path. Raw paths
would give one time series per product id and melt Prometheus.

Histogram buckets are shared across services and deliberately include every SLO threshold
so latency SLIs are exact rather than interpolated:

```
0.005, 0.01, 0.025, 0.05, 0.1, 0.2, 0.25, 0.4, 0.5, 0.8, 1.0, 2.5, 5.0, 10.0
        ^^^ catalog 200ms      ^^^ storefront 400ms   ^^^ recs 800ms
```

A `histogram_quantile` reading between buckets is an estimate; a `le="0.4"` ratio is a
count. SLIs use the ratio form for exactly that reason.

## SLI queries

Availability (error ratio — the numerator is *bad* events, Sloth's convention):

```promql
sum(rate(http_requests_total{service="<svc>", status_code=~"5.."}[{{.window}}]))
/
sum(rate(http_requests_total{service="<svc>"}[{{.window}}]))
```

Latency (ratio of requests slower than the threshold):

```promql
1 - (
  sum(rate(http_request_duration_seconds_bucket{service="<svc>", le="<threshold>"}[{{.window}}]))
  /
  sum(rate(http_request_duration_seconds_count{service="<svc>"}[{{.window}}]))
)
```

Both exclude `/healthz`, `/readyz`, `/metrics` and `/internal/*` — kubelet probe traffic is
not user traffic, and counting it would inflate the SLI with guaranteed-successful requests.
Excluded at the source: those routes are not recorded by the metrics middleware at all.

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `SERVICE_NAME` | *(required)* | Identity in metrics, logs, traces |
| `SERVICE_VERSION` | `dev` | Surfaced as a resource attribute |
| `LOG_LEVEL` | `INFO` | |
| `FAULT_ERROR_RATE` | `0.0` | Probability [0,1] a request returns 500 |
| `FAULT_LATENCY_MS` | `0` | Extra latency added before handling |
| `FAULT_LATENCY_RATE` | `1.0` | Probability the extra latency applies |
| `OTEL_SDK_DISABLED` | `false` | `true` in tests and CI |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | — | e.g. `http://otel-collector.monitoring:4318` |
| `CATALOG_URL` | — | storefront → catalog |
| `RECS_URL` | — | storefront → recs |
| `DATABASE_URL` | — | catalog → postgres |
| `DB_POOL_MAX` | `10` | catalog: asyncpg pool size (floor of 2, matching its min_size) |
| `RECS_BASE_LATENCY_MS` | `250` | recs: baseline simulated work |
| `RECS_TAIL_LATENCY_MS` | `400` | recs: extra latency on tail requests |
| `RECS_TAIL_RATE` | `0.02` | recs: fraction of requests hitting the tail |

`FAULT_*` are the load-bearing ones: the same three variables drive the chaos experiments
*and* the deliberately broken canary. One mechanism, two demos.

## SLO targets

Chosen so error budgets are actually burnable inside a demo. A 99.99% target over 30 days
leaves 4m 20s of budget — you cannot show a burn-rate alert against that in an afternoon.

| Service | Availability | Latency |
|---|---|---|
| `storefront` | 99.5% / 30d | 99% of requests < 400ms |
| `catalog` | 99.9% / 30d | 99% of requests < 200ms |
| `recs` | 99.0% / 30d | 99% of requests < 800ms |

`recs` gets the loosest availability target because it is deliberately the slow
dependency and storefront degrades around it; `catalog` the tightest because it does the
least work and its failures propagate.

The latency objectives are 99%, not the 95% they started at, and the reason is worth
stating because it is the sort of thing that is easy to get wrong by not doing the
arithmetic. The page threshold is `14.4 × (1 − objective)`. At 95% that is **72% of
requests slower than the threshold** — an alert that stays silent until nearly three
quarters of traffic is over the line, and that needs 43 minutes of total breach before
its 1h window agrees. At 99% the threshold is 14.4%, and a real regression pages in
minutes. A loose objective does not make a service more forgiving; it makes its alert
useless.

## Burn-rate alert windows

Standard multi-window multi-burn-rate, generated by Sloth (Google SRE Workbook table):

| Severity | Burn rate | Long window | Short window | Budget consumed before firing |
|---|---|---|---|---|
| page | 14.4x | 1h | 5m | 2% |
| page | 6x | 6h | 30m | 5% |
| ticket | 3x | 24h | 2h | 10% |
| ticket | 1x | 72h | 6h | 10% |

The short window exists to stop an alert that has already recovered from continuing to fire
off the long window's tail.

## Logs

JSON to stdout, one object per line. Reserved fields: `timestamp`, `level`, `service`,
`message`, `trace_id`, `span_id`. `trace_id` is what makes a Loki log line clickable
through to its Tempo trace, so it is present on every request-scoped log.

Records go to **two** sinks. stdout keeps `kubectl logs` working when the collector or
Loki is down — it is what you read while debugging the telemetry pipeline itself. The
OTel SDK's logging handler exports the same records over OTLP to the collector, and on to
Loki; that path is the one that carries trace context, because the handler reads
`trace_id` off the active span rather than re-parsing it out of a JSON string.

Most clusters instead run a node-level agent tailing container stdout, which also catches
output from a process that dies before flushing. That was not used here because it needs
a second, DaemonSet collector beside the gateway, and because scraping stdout to recover
a trace id the process already had is a longer route to the same place. The cost is
recorded rather than hidden: a line emitted during a hard crash may never be exported.

In Loki the service label derives from the OTLP resource attribute `service.name`, which
arrives as **`service_name`** — not a Kubernetes label name. Queries start
`{service_name="storefront"}`.

## Dependency metrics

`dependency_requests_total` and `dependency_request_duration_seconds` cover every
outbound call, whatever the protocol:

| Service | `dependency` values |
|---|---|
| `storefront` | `catalog`, `recs` (HTTP) |
| `catalog` | `postgres` (SQL) |
| `recs` | none — it calls nothing |

One metric family for HTTP and SQL alike so that "catalog is slow because Postgres is
slow" reads the same way on a dashboard as "storefront is slow because recs is slow".
Without the database timings, catalog's dashboard could only show total handler latency,
which cannot separate a slower query from a slower service — the exact distinction the
Postgres pod-kill scenario turns on.

`outcome` is `ok`, `timeout`, `error`, `transport_error`, or `http_4xx`/`http_5xx`. A
404 or an out-of-stock rejection counts as `ok`: the dependency answered correctly, and
counting it as a failure would make the dependency error rate track client behaviour.

## Traces

Service names match `SERVICE_NAME`. Span names use route templates. The storefront →
catalog → postgres and storefront → recs chains are the two traces the demo relies on.
