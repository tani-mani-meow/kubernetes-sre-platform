# 4. Replica-weighted canary, without a service mesh

Status: Accepted (2026-09-01)

## Context

The platform needs a deploy that catches a bad version and rolls it back without a human,
because "the errors stopped without anyone touching it" is the claim `evidence/` is
supposed to substantiate.

There are two ways to give a canary a traffic weight. A service mesh splits at the proxy
and can hit any percentage, route by header, and mirror traffic. Argo Rollouts on its own
splits by replica count: put N canary pods and M stable pods behind one Service and the
weight is N/(N+M).

## Decision

Argo Rollouts, no mesh. `gitops/services/storefront/base/rollout.yaml`:

- `replicas: 4`, steps `setWeight 25 → pause 2m → 50 → pause 2m → 75 → pause 1m`
- `maxSurge: 1`, `maxUnavailable: 0` — full capacity through a rollout, so a routine
  deploy does not burn error budget and make the budget useless as a signal
- analysis runs once for the whole rollout from `startingStep: 1`, not per step, so the
  measurement window is not reset at every promotion
- `canary-hash` comes from `valueFrom.podTemplateHashValue: Latest`

Four replicas rather than two: canary weight is quantised to 1/replicas, and at two
replicas the smallest possible canary is half of production traffic, which is not a
canary.

The Service selector stays `app.kubernetes.io/name: storefront` only, so canary and stable
pods are both endpoints and kube-proxy performs the split. Only the analysis needs to tell
them apart, and it does so through `rollouts_pod_template_hash` (ADR 0002).

## Consequences

The split is approximate. "25%" means one endpoint of four; kube-proxy distributes roughly
evenly across endpoints, not exactly per request. That is adequate for a gate set at 5%
and would not be adequate for an experiment measuring a 1% difference.

No per-request routing, no header-based canary, no traffic mirroring, no mesh-level retry
budget. Nothing in this project needs any of them.

Dilution is why the hash label is load-bearing. An unscoped query blends both populations:
at 25% weight, a canary failing a fraction `d` of its requests reads as `0.25 d` across the
Service. The `failureCondition` is `> 0.05`, so anything with `d` below 0.20 stays inside
the gate — a canary four times worse than the threshold allows would be promoted. Scoping
the query to the canary hash removes the factor of four.

The measurement window at the first step is small enough to reason about. The load
generator offers a constant 8 req/s to storefront, so one canary pod of four receives
roughly 2 req/s and the analysis' `[2m]` query covers about 240 requests. That is what
makes a 5% threshold with `failureLimit: 2` a signal rather than a coin flip; a single
unlucky 500 in a 60-request window is 1.7%, well inside the threshold.

Removing the mesh removes a subsystem whose own failure modes — proxy restarts, config
propagation lag, mTLS handshake cost, sidecar startup ordering — land as added latency and
5xx in exactly the series every SLO here measures. In a project whose deliverable is
measurements, that noise is the expensive part.

`abortScaleDownDelaySeconds: 30` and an explicit `kubectl argo rollouts abort` in
`chaos/experiments/canary-abort.sh` mean a failed analysis returns traffic to stable
rather than parking the rollout in a degraded state waiting for a human. The cost is that
an aborted Rollout stays `Degraded` until a new template arrives, so Argo CD shows the
Application unhealthy until someone promotes or reverts — `make rollout-retry` exists for
that.

## Rejected

**Istio or Linkerd traffic splitting.** Finer weights, header-based routing, and mirroring,
plus the mesh's own golden-signal telemetry. It puts a proxy in the request path of every
SLI in the project and makes its failures indistinguishable from the injected ones.

**A Deployment plus a second Service with weights edited by hand.** Cheaper still, and it
demonstrates nothing: the automated abort is the point, and performing the rollback
manually is what the project is arguing against.
