# 3. Compile SLOs with Sloth at build time

Status: Accepted (2026-09-01)

## Context

Three services with an availability and a latency SLO each is six SLOs. Each one needs
seven rate recording rules (5m, 30m, 1h, 2h, 6h, 1d, 3d), a 30-day aggregate, six meta
rules, and two alerting rules whose expressions each contain four window/threshold pairs.
`gitops/slo/generated/storefront-rules.yaml` is 469 lines for one service.

Writing that by hand is not hard, it is just uniformly error-prone in the one way that
matters: a typo in one window of one expression produces an alert that never fires and
reports nothing. There is no test that catches it and no dashboard that shows it.

## Decision

`make slo-gen` (`scripts/slo-gen.sh`) runs Sloth v0.16.0 over `gitops/slo/specs/*.yaml`
and writes `gitops/slo/generated/*.yaml`. The output is committed.
`gitops/clusters/*/apps/slo-rules.yaml` syncs that directory at sync-wave 4 — after
kube-prometheus-stack has established the `PrometheusRule` CRD (wave 0) and before the
application services (waves 5–8), so the SLI recording rules are already evaluating when
the first request is served.

The specs use Sloth's `sloth.slok.dev/v1` `PrometheusServiceLevel` form rather than the
raw `prometheus/v1` form. This cluster runs prometheus-operator, so the CRD form compiles
straight to a `PrometheusRule` object Argo CD can sync; the raw form emits a bare
`groups:` document that would need wrapping in a ConfigMap and a Prometheus reload to take
effect.

## Consequences

The alert arithmetic is a diff. A reviewer opening a pull request sees
`> (14.4 * 0.005)` and `> (6 * 0.005)` in context and can check them against the table in
`docs/slo-methodology.md` without running anything or trusting a controller.

Nothing extra runs in the cluster. No Sloth deployment, no `PrometheusServiceLevel` CRD to
install, no controller whose version has to track a chart, and nothing additional that can
be down at the moment the alerts matter.

Regeneration is manual, which is the obvious failure mode: nothing in the cluster reads
`gitops/slo/specs/`, so a spec edited without re-running `make slo-gen` would change the
documented intent and nothing else. Two checks close it. `scripts/validate.sh` regenerates
every spec into a temporary directory and `diff`s it against the committed output,
reporting `DRIFTED - run make slo-gen` and failing; `.github/workflows/manifests.yml`
installs Sloth v0.16.0 and runs that on every pull request touching `gitops/**`. Separately,
the harness tests cross-check that every alert name and `sloth_id` in
`chaos/scenarios.yaml` exists in the generated rules — a rename on one side fails a test
rather than producing a chaos run that waits ten minutes for an alert that can never fire.

The residual gap is local only: `validate.sh` skips the drift check with a warning when
Sloth is not installed, so a developer without it sees "everything renders" while CI
disagrees.

Sloth is a build-time dependency only. `docs/prerequisites.md` lists it as needed for
`make slo-gen` and nothing else, so a reviewer running `make demo` never has to install it.

The generated files carry `# DO NOT EDIT` and `app.kubernetes.io/managed-by: sloth`. The
`slo-rules` Application has `selfHeal: true`, which closes the other half of the hole:
someone silencing an SLO by editing the rule in-cluster has the edit reverted within
seconds.

## Rejected

**Hand-written PrometheusRules.** Total control and no build step. Sixty-odd rules whose
correctness cannot be reviewed by reading them, and whose most likely defect is silence.

**A Sloth controller in the cluster** watching `PrometheusServiceLevel` resources and
writing `PrometheusRule` objects. This is the mode Sloth is usually deployed in and it
removes the manual regeneration step. It adds a Deployment, a CRD and a reconciliation
loop to earn back an action nobody performs during a demo, and it moves the generated
rules out of Git — which costs the reviewability that was the reason for using a generator
in the first place.
