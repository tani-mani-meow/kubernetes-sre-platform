# 8. No Argo CD in CI

Status: Accepted (2026-09-01)

## Context

CI needs to answer one question about the application: do the three services actually work
together against a real Postgres? Two ways to get there in an ephemeral cluster — install
Argo CD, point it at the branch, and wait for it to converge; or apply a purpose-built
Kustomize overlay with `kubectl`.

The first is tempting because it exercises the delivery path. It also takes several
minutes per pull request to prove that Argo CD works, which nobody doubts.

## Decision

A `ci` overlay per service (`gitops/services/*/overlays/ci/`), applied by
`.github/workflows/e2e-kind.yml` with `kubectl apply -k` in dependency order — postgres,
catalog, recs, storefront — on a single-node `kind` cluster. The Argo Rollouts controller
is installed first, so CI exercises the same workload kind the real clusters run rather
than a CI-only Deployment variant; without it the Rollout would be accepted and then never
create a pod.

The overlay differs from `kind` only by removing what CI cannot or should not run:

- **PodMonitor deleted** with a `$patch: delete`. prometheus-operator's CRDs are not
  installed, and `kubectl apply` rejects an unknown kind outright — it would fail the whole
  overlay rather than skip one object.
- **`OTEL_SDK_DISABLED=true`.** No collector exists in the e2e cluster, and the OTLP
  exporter's retry backoff would show up as latency in the very requests the test asserts
  on.
- **`FAULT_ERROR_RATE=0.0` and `FAULT_LATENCY_MS=0` restated rather than inherited.** The
  canary demo works by committing a non-zero rate into the base, and on the day someone
  does that, e2e must not start failing for a reason unrelated to the commit under test.
- **storefront's Rollout is patched down to 2 replicas, the analysis removed and the steps
  replaced with a single `setWeight: 100`.** A first deploy has no stable version to canary
  against, so the analysis would only make the job wait out the pause steps. (The replica
  count comes from that patch rather than Kustomize's `replicas:` transformer, which only
  recognises built-in workload kinds and errors on a Rollout — and the patch is JSON6902
  rather than a strategic merge, because Kustomize has no schema for a CRD and would replace
  the `containers` list instead of merging into it.)

Delivery to real clusters stays in Git. `.github/workflows/app-ci.yml` ends with a
`bump-gitops` job that runs `kustomize edit set image` in
`gitops/services/*/overlays/eks` and commits the result; Argo CD converges the cluster.

## Consequences

Nothing in the pipeline holds cluster credentials. The workflow's `permissions` are
`contents: read` plus `packages: write` for the build job and `contents: write` for the
bump. That separation is the actual GitOps claim, and it is checkable by reading one file.

Argo CD's value in this project is behavioural: drift reverted by `selfHeal` while a
reviewer watches, fourteen Applications ordered by sync waves, the whole cluster expanding
from one `kubectl apply` of a root app-of-apps. All of that is demonstrated live in
`docs/demo-script.md` and measured by the `gitops-drift` chaos scenario. None of it is
better demonstrated by a PR job.

The `eks` overlays pin `newTag: sha-placeholder`, a tag that deliberately does not exist.
If the rewrite job ever fails to run, the deploy fails loudly at `ImagePullBackOff` rather
than silently redeploying whatever `latest` happens to be.

The commit loop that a self-committing workflow invites is guarded three independent ways,
because any one of them silently failing would cost real Actions minutes before anyone
noticed: disjoint path filters (`app/**` only), `[skip ci]` in the commit message, and the
fact that pushes made with `GITHUB_TOKEN` do not trigger workflows.

What the e2e job does assert is narrow and worth listing, because the narrowness is the
decision: the full call chain through Postgres, the metric names and `le=` bucket
boundaries the SLIs join on, route templates rather than raw paths, a pod kill recovering
on its own, an injected fault producing 500s *without* breaking `/healthz` or `/metrics`
(ADR 0007), and storefront degrading to `recommendations_degraded: true` when recs is
scaled to zero. Every one of those is a claim made elsewhere in this repository.

The cost is a third rendering of every service to maintain alongside `kind` and `eks`.
`.github/workflows/manifests.yml` runs `scripts/validate.sh` over all of them and then
`kubeconform` over the rendered output, so a drift between the three — or a Deployment
whose `replicas` became a string — fails there rather than at sync time.

## Rejected

**Install Argo CD in the CI cluster and let it sync.** Exercises the real delivery path.
Requires the branch under test to be pushed and reachable, adds minutes of convergence
waiting to every pull request, and makes the e2e job's failures ambiguous between "the app
is broken" and "Argo CD had not finished".

**Deploy to a real cluster from CI with `kubectl` and stored kubeconfig credentials.** The
common arrangement, and it is precisely what GitOps replaces: it puts write access to the
cluster in the pipeline, and makes the cluster's actual state something no one can read
from Git.
