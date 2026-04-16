# 1. Develop on kind, capture evidence on EKS

Status: Accepted (2026-09-01)

## Context

The portfolio this repository joins already contains three build-time projects: Terraform
provisioning, a pipeline that ships a container to ECS Fargate, and an AWS landing zone.
All three stop at the moment something is deployed. What is missing is anything that
runs, breaks, and recovers — and any Kubernetes at all.

The deliverable is therefore `evidence/`: a measured time-to-detect and MTTR for each
failure mode. Producing that is not a single run. It is writing a scenario, discovering
the alert never fires, changing a threshold or a window, and running it again — dozens of
times across days.

On EKS that iteration is metered continuously: control plane, node group, and with a
conventional VPC a pair of NAT gateways (see ADR 0006). Every debugging session becomes a
billed one, and the failure mode is predictable — the cluster gets left running overnight
once and the sign-up credits funding the work are gone.

## Decision

All development happens on `kind`. `infra/kind/kind-config.yaml` defines one control-plane
node (which also hosts ingress-nginx via `extraPortMappings` on 80/443) and two workers,
all pinned to `kindest/node:v1.34.11` by digest.

EKS exists only as short capture sessions: `make eks-up`, run the suite, `make eks-down`.
The Kubernetes minor is held equal in both environments — kind's node image is 1.34 and
`infra/terraform/variables.tf` defaults `kubernetes_version` to match — so an API removal
between minors cannot be the thing that surfaces for the first time on the paid cluster.

The two kind workers carry `topology.kubernetes.io/zone: kind-a` and `kind-b`. There is no
fault isolation behind those labels; both are containers on one Docker daemon. They exist
so `chaos/experiments/az-loss.sh` — which cordons and drains every node carrying a given
zone label — can be written, debugged and dry-run locally, and needs no changes on EKS
where the same label is set by the cloud provider and means something.

## Consequences

`make demo` on a reviewer's laptop is the same path the author develops on, so it stays
working by constant use rather than by being written once and hoped over.

kind cannot reproduce: real zone isolation, EBS detach/attach latency, LoadBalancer
provisioning, or IRSA. `az-loss` is the only scenario marked `clusters: [eks]` for exactly
that reason; everything else runs in both.

Prometheus retention is 24h in both environments
(`gitops/platform/kube-prometheus-stack/values-*.yaml`), so the 24h and 72h ticket
burn-rate windows never have a full window of data behind them in either environment. The
1h/5m and 6h/30m page windows are the ones a demo can actually drive, and they are the
ones the chaos scenarios target. This is stated in `docs/slo-methodology.md` rather than
hidden.

Every row in `evidence/README.md` carries its cluster, because a kind MTTR and an EKS MTTR
are not the same measurement. Volume attach time in particular differs by an order of
magnitude, and `pod-kill-postgres` is dominated by it.

Windows hosts pay a setup cost before any of this runs: WSL2 and Docker Desktop need an
elevated install, and the host inotify limits have to be raised or Grafana and Loki
crash-loop with `too many open files`. Both are documented in `docs/prerequisites.md` and
checked by `scripts/doctor.sh` and `scripts/kind-up.sh`.

## Rejected

**Build on EKS from day one.** Matches the original brief and removes any doubt about
whether the platform works on real nodes. It bills a control plane, a node group and NAT
for the entire development period, and it makes reproducibility a separate feature to
build rather than a by-product.

**Leave a long-lived EKS cluster running between sessions.** Cheaper in attention, far more
expensive in money, and it removes the forcing function that keeps `make eks-up` /
`make eks-down` working end to end.
