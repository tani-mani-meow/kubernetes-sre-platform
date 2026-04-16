# 7. Fault injection exempts the probe and control endpoints

Status: Accepted (2026-09-01)

## Context

`FAULT_ERROR_RATE` and `FAULT_LATENCY_MS` are applied by a middleware inside the same
ASGI app that serves `/healthz`, `/readyz`, `/metrics` and `/internal/faults`. Middleware
order in `svc_common.app.create_app` puts the fault layer inside the metrics layer, so
injected 500s are counted and injected latency is measured — a fault the SLI cannot see is
not worth injecting.

Kubernetes probes and the Prometheus scrape are ordinary HTTP requests to that same
server. If the fault middleware treated them like user traffic, a 30% error injection
would fail roughly 30% of liveness probes.

## Decision

```python
EXEMPT_PREFIXES = ("/healthz", "/readyz", "/metrics", "/internal/")
```

`FaultInjector.should_error` and `FaultInjector.latency_for` return early for those paths.
`svc_common.metrics.make_metrics_middleware` skips the same prefixes using the same
constant, so the exclusion from the SLI and the exemption from faults cannot drift apart.

## Consequences

A fault cannot get a pod restarted. With `livenessProbe` at `periodSeconds: 10` and
`failureThreshold: 6`, roughly a minute of consecutive failures would trigger a restart —
and a restart ends the fault, because the config lives in the process. The experiment would
then have measured Kubernetes healing itself rather than the SLO burn it was pointed at.
The tolerant liveness thresholds and this exemption are the same decision expressed twice.

The same exemption keeps guaranteed-successful traffic out of the denominator. For
storefront on kind: readiness every 5s and liveness every 10s across four pods is 1.2
req/s, plus a `/metrics` scrape every 15s per pod is another 0.27 req/s — about 1.5 req/s
against roughly 8 req/s of user traffic. Counting it would deflate every error ratio by
around 16% and every latency ratio by more, since probes are the fastest requests the
service handles. `docs/conventions.md` records this as excluded *at the source* rather
than as a `route!=` matcher in PromQL, so no SLI query can forget it.

`/internal/*` is exempt so the chaos harness can always turn a fault back off. An injector
that could be blocked by its own injection is a trap, and `injectors.py` cleans up in a
`finally` block precisely because a fault left running poisons every measurement after it.

`gitops/services/storefront/base/ingress.yaml` is a two-prefix allowlist (`/products`,
`/orders`) rather than `/`. A `/` rule would publish `/internal/faults` to anything that
can reach the load balancer, and anyone who could reach it from outside could dial in the
exact failure the evidence claims was injected under controlled conditions.

The consequence to remember when reading a dashboard: **a pod serving 100% injected 500s
still reports Ready.** Pod status is not a fault indicator in this platform. The SLI is.
Both runbooks say so.

Chaos Mesh faults are not covered by this exemption and deliberately so.
`chaos/experiments/latency-catalog.yaml` is a `NetworkChaos` with `direction: to`, which
delays packets to catalog including probe traffic — but 500ms against a
`readinessProbe.timeoutSeconds: 2` does not breach the timeout, so the pods stay Ready and
the fault stays visible as latency rather than as pod churn. That the fault is invisible in
catalog's own logs and traces is the point of that scenario: finding it requires comparing
storefront's `dependency_request_duration_seconds` against catalog's own
`http_request_duration_seconds` and noticing the gap.

## Rejected

**Fault everything, including probes.** More faithful to a real process failure, and it
turns every experiment into a test of kubelet's restart behaviour with the SLO measurement
buried underneath it.

**Fault probes but loosen the liveness thresholds to compensate.** Trades a clean rule for
a tuning exercise that would have to be redone for every new fault magnitude, and leaves
the SLI denominator polluted either way.
