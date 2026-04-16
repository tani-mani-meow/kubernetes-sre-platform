# Architecture Decision Records

| # | Decision |
|---|---|
| [0001](0001-develop-on-kind-capture-on-eks.md) | All development happens on `kind`; EKS exists only for short evidence-capture sessions |
| [0002](0002-metrics-bypass-the-otel-collector.md) | Traces and logs go through the OTel Collector; metrics are scraped per pod |
| [0003](0003-compile-slos-with-sloth-at-build-time.md) | Sloth compiles the SLO specs at build time and the output is committed |
| [0004](0004-replica-weighted-canary-without-a-service-mesh.md) | Argo Rollouts weights the canary by replica count; no service mesh |
| [0005](0005-single-replica-postgres.md) | Postgres is a one-replica StatefulSet, not an operator |
| [0006](0006-no-nat-gateway-in-the-capture-vpc.md) | The EKS capture VPC has no NAT gateway and puts nodes in public subnets |
| [0007](0007-fault-injection-exempts-probe-endpoints.md) | Injected faults never reach `/healthz`, `/readyz`, `/metrics` or `/internal/*` |
| [0008](0008-no-argo-cd-in-ci.md) | CI applies a `ci` Kustomize overlay with `kubectl`; Argo CD is demonstrated live |

Each record carries a "Rejected" section. Those are the load-bearing part: this project
is small enough that most of what makes it defensible is what was deliberately left out
— no service mesh, no Postgres operator, no in-cluster SLO controller, no NAT gateway.
A reader can check the accepted decisions against the manifests; the rejected ones are
the only place the reasoning survives.
