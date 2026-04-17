# Planning

Decisions that shaped the project, and the alternatives that lost. Per-decision detail
lives in `docs/adr/`; this is the index and the reasoning that spans decisions.

## What this project is for

The portfolio it joins already has three build-time repos: Terraform provisioning, a
pipeline that ships a container to ECS Fargate, and an AWS landing zone. All three stop
at the moment something is deployed. Two gaps follow from that: no Kubernetes, and
nothing that runs, breaks, and recovers.

So the goal is not "deploy an app to Kubernetes". It is to produce evidence of Day-2
operations: an SLO with a real error budget, an alert that fired because a budget was
burning, a canary that aborted itself, and a measured recovery time for each failure
mode. The `evidence/` directory is the deliverable; everything else exists to generate it.

## Approaches considered

**Application stack.** Go was the strongest signal for a platform role and the natural
language of the Kubernetes ecosystem. Python/FastAPI was chosen instead: the app is
deliberately scenery, and a codebase the author can defend line-by-line in an interview
beats a more impressive one they cannot. Adapting an existing demo app (Online Boutique)
was rejected — "did you write this?" is a bad question to face, and injecting a
controllable slow dependency into someone else's app is harder than writing 150 lines.

**Build order.** Building directly on EKS matches the original brief but bills a control
plane, nodes and NAT for the whole development period. Everything is developed on `kind`
instead, at zero cost; EKS appears only as a short evidence-capture phase funded by
sign-up credits. The reproducibility story (`make demo` on a reviewer's laptop) falls out
of this for free, which is itself a hiring signal.

**Repo layout.** A split app-repo/config-repo pair is how larger orgs actually separate
concerns. A single monorepo was chosen anyway: one link for a recruiter, one README, one
`make demo`. The cost is that CI's image-tag bump commits could retrigger app CI; that is
handled with path filters and is worth a paragraph in the README rather than a second repo.

## Locked decisions

| Decision | Choice | Rejected |
|---|---|---|
| App stack | Python 3.12 / FastAPI, 3 services + Postgres | Go, Node, forking a demo app |
| Cluster for development | `kind`, 1 control-plane + 2 workers | EKS from day one |
| Cluster for evidence | EKS, 2-3 short capture sessions, destroyed after each | leaving a cluster running |
| Repo | single monorepo, public from day one | app + gitops split |
| GitOps | Argo CD, app-of-apps, `selfHeal: true` | Flux, `kubectl apply` in CI |
| Metrics | in-process Prometheus client, scraped per pod | routing metrics via OTel Collector |
| Traces / logs | OTel SDK to a Collector gateway, Tempo + Loki | direct exporters per service |
| SLO rules | Sloth spec compiled at build time, output committed | hand-written rules, in-cluster generator |
| Progressive delivery | Argo Rollouts, replica-weighted canary | service mesh traffic splitting |
| Chaos | Chaos Mesh CRDs + `kubectl` scripts for node operations | Litmus, ad-hoc manual breakage |
| Database | plain StatefulSet + seed Job | CloudNativePG or another operator |
| Ingress | ingress-nginx in both environments | AWS Load Balancer Controller / ALB |
| Local images | `kind load docker-image` | a local registry container |

## Deliberately not doing

No service mesh, no Thanos, no multi-cluster, no external secret manager, no
authentication. Each of these would add a subsystem that generates no additional
evidence. Where the omission is interesting, it is recorded in `docs/adr/` as
"considered, rejected, why" — which is more useful in an interview than the feature
would have been.

## Known limitations, recorded rather than hidden

**The 6h/72h ticket burn-rate window cannot be demonstrated.** Prometheus retains 24h on
kind and 24h on the capture cluster, and a capture session lasts about four hours. The
rule exists and is correct; there is simply never 72 hours of history behind it. Worth
knowing that `rate()` degrades rather than fails here — over a range longer than the
retained data it computes across whatever exists, so an under-retained window publishes a
plausible number for a shorter period rather than reporting no data. That is why kind
retention is 24h rather than the 6h that would otherwise be enough: it makes the 2h/24h
window honest instead of silently four times shorter than its name.

**Every measured MTTR is pessimistic by roughly one rate window.** Recovery is declared
when the burn rate settles under 1.0, and the SLIs use 5-minute rate windows, so the clock
keeps running for several minutes after the system is actually healthy. The alternative -
measuring to the moment the fault was removed - would flatter the numbers by exactly the
amount that matters. The evidence README states the definition rather than picking the
kinder one.

**A single Prometheus is a single point of failure for its own alerting.** No Thanos, no
federation, no dead-man's-switch beyond the chart's default Watchdog alert. On a cluster
whose purpose is to be broken on camera, an alerting stack that could itself be taken out
by an experiment is a real gap; it is out of scope here and would be the first thing to
add for anything longer-lived.

## Open items

- Docker Desktop and WSL2 need an elevated install on the build machine; until then any
  step that requires a running cluster is unverified. Tracked in `docs/prerequisites.md`.
- SLO targets (99.5 / 99.9 / 99.0) are set so error budgets are burnable inside a demo.
  They should be re-derived from observed baseline behaviour once the stack has run under
  the load generator for a few hours, and the revision recorded here.
