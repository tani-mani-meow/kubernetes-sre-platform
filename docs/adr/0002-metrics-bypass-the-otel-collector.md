# 2. Metrics bypass the OTel Collector

Status: Accepted (2026-09-01)

## Context

The three services emit traces, logs and metrics. Traces and logs go through an OTel
Collector gateway in `monitoring` and out to Tempo and Loki. The symmetric arrangement —
one SDK, one wire protocol, one gateway, three backends — is the one every OpenTelemetry
diagram draws, and it is the obvious thing to do with metrics too.

It breaks the canary demo.

Argo Rollouts' canary analysis
(`gitops/services/storefront/base/analysistemplate.yaml`) selects canary pods with
`rollouts_pod_template_hash="{{args.canary-hash}}"`, a value the controller resolves from
the canary ReplicaSet. That label reaches Prometheus only because the PodMonitor copies it
off the pod:

```yaml
podTargetLabels:
  - rollouts-pod-template-hash
```

A gateway collector receives OTLP over the network. It sees a sender address, not a set of
pod labels; `k8sattributes` can recover `k8s.pod.name` and namespace from the IP, but the
Rollouts hash is a pod label and the exported series are attributed to the collector's own
identity. Without per-pod attribution the analysis query cannot separate canary pods from
stable ones.

## Decision

`svc_common.metrics` builds a `CollectorRegistry` per application and exposes `/metrics`
on the service port (8000, the same port as the API — one `PodMonitor` port name, no
second listener to keep alive). Prometheus scrapes each pod directly through a PodMonitor
per service, every 15s.

The collector has no metrics pipeline. `gitops/platform/otel-collector/values-*.yaml` sets
`service.pipelines.metrics: null` and nulls the chart's default `prometheus` receiver with
it, so the collector no longer scrapes its own 8888 endpoint into a pipeline. The
collector's own telemetry still reaches Prometheus, through a ServiceMonitor.

## Consequences

Canary analysis can compare canary pods against stable ones, which is the entire content
of ADR 0004's abort behaviour.

The failure mode if `podTargetLabels` were ever dropped is worth knowing: the label would
not exist on any series, the analysis query would match nothing, and
`failureCondition: "isNaN(result[0]) || ..."` treats no-data as a failure — so every canary
would abort. Noisy, but fail-safe rather than fail-open.

Metrics and traces are correlated by convention rather than by plumbing: the `service`
label on every metric equals `SERVICE_NAME`, which equals the `service.name` resource
attribute on every span (`docs/conventions.md` fixes this). Nothing enforces it; a
mismatch would silently split a service in two on the dashboards.

Two telemetry paths have to keep working instead of one. The compensation is the right
one for this project: if the collector is down, traces and logs stop, but metrics, SLIs,
recording rules, alerts and canary analysis all keep working. The measurements are the
deliverable, so they should be the thing that survives.

Bucket boundaries are chosen in application code (`LATENCY_BUCKETS` in
`svc_common.metrics`) rather than by a collector processor, so the three SLO thresholds
(0.2s, 0.4s, 0.8s) are guaranteed to be exact bucket edges. See `docs/slo-methodology.md`
for why that matters.

## Rejected

**Route metrics through the collector for pipeline symmetry.** A cleaner diagram, one
exporter configuration, and the option of collector-side processing. It would have
silently broken the canary analysis — silently being the operative word, since the query
would return a plausible blended number rather than an error.

**Run the collector as a DaemonSet agent so each instance sees only local pods.** Recovers
some pod identity, but multiplies the collector instance count by the node count, splits
the `k8sattributes` cache, and still does not carry the Rollouts hash without extra
configuration. It solves a problem that only exists because of the first rejected option.
