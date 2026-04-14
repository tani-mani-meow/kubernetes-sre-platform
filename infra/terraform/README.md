# EKS evidence-capture cluster

Terraform for the short-lived EKS cluster used to capture the artefacts in `evidence/`.

This is not the development environment. Everything in this project is built and rehearsed
on `kind` (`infra/kind/kind-config.yaml`) at zero cost. EKS exists to answer the one question
kind cannot: does this work on a real managed control plane, with real availability zones,
real EBS, and a real load balancer. A session lasts about 4.5 hours and ends with
`terraform destroy`.

Everything below follows from that: the cluster is disposable, so it is optimised for cost
and for being completely removable, and several of its choices would be wrong in production.
Those are marked.

---

## Cost model

List prices, `us-east-1`, August 2026. Verify against the pricing pages before relying on
them — these are the numbers the design was reasoned from, not a quote.

| Item | Rate | Hourly |
|---|---|---|
| EKS control plane | $0.10/hr | $0.1000 |
| 3 × `t3.large` on-demand | $0.0832/hr each | $0.2496 |
| Node root volumes, 3 × 50 GiB gp3 | $0.08/GiB-month | $0.0164 |
| PVCs (Prometheus, Loki, Tempo, Postgres), ~60 GiB gp3 | $0.08/GiB-month | $0.0066 |
| NLB for ingress-nginx | $0.0225/hr + ~1 NLCU | $0.0285 |
| Public IPv4 — 3 nodes + 2 NLB addresses | $0.005/hr each | $0.0250 |
| NAT gateway | *not used* | $0.0000 |
| CloudWatch control plane logs | *disabled* | $0.0000 |
| **Total** | | **≈ $0.43/hr** |

**A 4.5-hour capture session costs about $1.90.** Three sessions is roughly $6, against the
~$100 of sign-up credits funding this work — around 6%.

Data transfer out is small enough to disappear into the 100 GB/month free allowance. Nothing
in the stack egresses meaningfully; the load generator runs inside the cluster.

### The number that actually matters

The session is cheap. What can consume the budget is a resource that outlives it:

| Left running for 30 days | Cost |
|---|---|
| 3 × `t3.large` | ~$180 |
| EKS control plane | ~$72 |
| NAT gateway, had one been used | ~$32 per AZ |
| Orphaned NLB (+ its 2 public IPs) | ~$23 |
| Orphaned EBS volumes, 150 GiB | ~$12 |

One forgotten cluster costs more than every planned session combined, by two orders of
magnitude. The teardown discipline below is therefore the most important part of this
directory — more important than anything in the `.tf` files. It is also why the NAT gateway
was removed: at ~$32/month per AZ it was both the largest line item and, being invisible in
the console unless looked for, an easy one to leave behind.

---

## Deliberate trade-offs

Each of these is a demo-only decision with a documented production counterpart.

**No NAT gateway; nodes in public subnets with public IPs.** Two NAT gateways would have cost
roughly $0.09/hr — comparable to the entire EKS control plane, for a cluster that lives four
hours. The cost is that every node is directly addressable from the internet, protected only
by its security group. Acceptable for a tagged throwaway cluster holding no real data.
*In production:* nodes in the private subnets this module already creates and leaves empty,
`enable_nat_gateway = true`, and `subnet_ids` on the node group pointed at
`module.vpc.private_subnets`. Where egress is only to AWS APIs, interface VPC endpoints for
ECR, STS, EC2, ELB and CloudWatch Logs plus an S3 gateway endpoint remove most NAT traffic —
though at ~$0.01/hr per endpoint, seven of them cost more than a NAT gateway over four hours,
which is why this cluster uses neither.

**`ON_DEMAND`, not `SPOT`.** The instinct for a demo cluster is spot. It is wrong here. The
deliverable is measured recovery time from deliberate, controlled failures; a spot
reclamation is an uncontrolled node termination, and once one lands mid-experiment there is
no way to tell whether the MTTR recorded belongs to the chaos experiment or to AWS taking
capacity back. Spot saves about $0.68 per session on a session costing under two dollars.
Silently worthless evidence is more expensive than that.

**No control plane logging.** `enabled_log_types = []`, and no CloudWatch log group. Nothing
in the evidence pipeline reads control plane logs — every SLI, alert and chaos measurement
comes from in-cluster Prometheus, Loki and Tempo. The audit log is the chattiest of the five
and is billed on ingestion, so its cost would scale with how hard the cluster is driven,
which is precisely what a capture session does. A log group created outside the module also
survives `terraform destroy`. *In production:* `audit` and `authenticator` at minimum, with a
retention policy — there they are the only record of who changed what.

**Local state, no S3 backend.** One operator, one machine, a four-hour resource lifetime. An
S3 backend needs its own bucket bootstrapped first: a permanent AWS footprint created to
protect something designed to be impermanent, and one more thing to remember to delete.
Remote state earns its keep with concurrent operators or CI, and there are neither.

The failure mode is real: **if `terraform.tfstate` is lost while a cluster is running,
`terraform destroy` cannot reach the resources**, and the cluster has to be dismantled by
hand through the console while the control plane bills $0.10/hr. Two things keep that
recoverable — every resource carries `Project` and `Ephemeral` tags, so Resource Groups gives
a complete inventory to work from; and this repository sits under OneDrive, so the state file
is replicated off the machine continuously. That is a backup, not a lock, and it works only
because exactly one machine ever writes it. State is gitignored at the repo root and must
stay out of the public repo.

**EKS Access Entries in `API` mode, not the aws-auth ConfigMap.** A fresh cluster has no
legacy tooling writing aws-auth, so there is nothing to stay compatible with. `API` mode
stops the ConfigMap being consulted at all, which removes the failure this project cannot
afford: a malformed aws-auth edit that cannot be reverted without the access it just
destroyed, whose only fix is destroy-and-recreate — an hour out of a four-hour session.
Access entries live in the control plane and are repairable with the AWS CLI even after
in-cluster access is gone. The trade-off is that there is no ConfigMap fallback, which is
why `additional_cluster_admin_arns` exists.

---

## Prerequisites

- Terraform >= 1.5.7
- AWS CLI v2, authenticated (`aws sts get-caller-identity` must succeed)
- `kubectl`
- The IAM identity running `terraform apply` becomes cluster-admin via an access entry. It
  needs permissions to create VPC, EKS, EC2, IAM and ELB resources.

---

## Session runbook

```bash
cd infra/terraform
cp terraform.tfvars.example terraform.tfvars    # then narrow the endpoint CIDR if practical

terraform init
terraform apply
```

Expect roughly 15–20 minutes: the control plane alone takes about 10, the node group another
5, and the addons follow.

**First apply may need two steps.** The `kubernetes` provider is configured from
`module.eks` outputs, which do not exist before the cluster does. Terraform usually defers
this correctly, but on an empty state it can fail with a provider configuration error on the
`storage.tf` resources. If that happens:

```bash
terraform apply -target=module.eks
terraform apply
```

This affects only the first apply of a fresh state.

Then:

```bash
$(terraform output -raw update_kubeconfig_command)
kubectl get nodes -L topology.kubernetes.io/zone
kubectl get storageclass          # gp3 (default), gp2 must NOT say (default)
```

The zone column is what the AZ-loss scenario selects on, and it should show two distinct
AZs. `terraform output availability_zones` gives the same values for scripting.

---

## Teardown

**Order matters. Running `terraform destroy` first is the mistake that costs money.**

### Why

`ingress-nginx` is a `Service type=LoadBalancer`. The NLB behind it is created by the
in-cluster AWS cloud provider in response to that Service — not by Terraform. It is not in
state, and Terraform will not delete it.

Destroy the cluster first and the cloud controller is gone before anything tells AWS to
remove the NLB. Two things then happen:

1. The NLB survives and keeps billing, invisible unless specifically looked for.
2. Its ENIs are still attached to the VPC subnets, so the VPC destroy fails with
   `DependencyViolation` after a long timeout — leaving a half-destroyed stack *and* a leaked
   load balancer.

The same applies to EBS volumes behind PVCs: they are created by the CSI driver, not by
Terraform.

### Ordered teardown

**1. Stop Argo CD re-creating what you delete.** `selfHeal: true` is set (see
`planning.md`), so deleting a Service directly just brings it back. Remove the Application
first:

```bash
kubectl -n argocd delete applications --all
```

**2. Delete every LoadBalancer Service, and let the cloud provider remove the NLB.**

```bash
kubectl get svc -A -o jsonpath='{range .items[?(@.spec.type=="LoadBalancer")]}{.metadata.namespace}{"/"}{.metadata.name}{"\n"}{end}'
kubectl -n ingress-nginx delete svc ingress-nginx-controller
```

**3. Confirm the NLB is actually gone before continuing.** This is the checkpoint that makes
the rest safe:

```bash
aws elbv2 describe-load-balancers --region "$(terraform output -raw region)" \
  --query 'LoadBalancers[].LoadBalancerName' --output table
```

Deletion takes a minute or two. Do not proceed while one is still listed.

**4. Delete PVCs so the CSI driver releases their EBS volumes.** The `gp3` class uses
`reclaimPolicy: Delete`, so this is enough — but only while the driver is still running.

```bash
kubectl delete pvc --all -n monitoring
kubectl delete pvc --all -n demo
```

**5. Destroy.**

```bash
terraform destroy
```

**6. Sweep for orphans.** Two commands, because there are two populations with two different
tags — Terraform's `default_tags` cover only what Terraform created, while cloud-provider
resources carry `kubernetes.io/cluster/<name>` instead. Sweeping only on `Project` is exactly
how an NLB gets missed.

```bash
terraform output -json orphan_sweep_commands
```

Run both. Both should return nothing.

**7. Confirm in the console.** Check EC2 → Load Balancers, EC2 → Volumes, and VPC → Your
VPCs. Billing data lags several hours, so the console is the faster check.

### If destroy already failed

Usually `DependencyViolation` on a subnet or the internet gateway, which means something with
an ENI is still in the VPC — nearly always a leaked NLB. Delete it by hand, then re-run:

```bash
aws elbv2 describe-load-balancers --region <region> \
  --query 'LoadBalancers[].[LoadBalancerName,LoadBalancerArn]' --output table
aws elbv2 delete-load-balancer --load-balancer-arn <arn>
terraform destroy
```

A namespace stuck in `Terminating` is a finalizer waiting on a controller that is already
gone; the objects it is blocking on need their finalizers cleared before the namespace
releases.

---

## Files

| File | Contents |
|---|---|
| `versions.tf` | Provider and Terraform constraints; the state backend decision |
| `providers.tf` | AWS `default_tags`, Kubernetes provider with exec auth |
| `variables.tf` | All inputs, each with its reasoning |
| `main.tf` | Locals, tags, AZ lookup |
| `vpc.tf` | VPC, subnets, the no-NAT decision |
| `eks.tf` | Cluster, access entries, addons, node group, EBS CSI IRSA |
| `storage.tf` | `gp3` default StorageClass; removes `gp2` as default |
| `outputs.tf` | Kubeconfig command, AZ list, NLB lookup, orphan sweep |
