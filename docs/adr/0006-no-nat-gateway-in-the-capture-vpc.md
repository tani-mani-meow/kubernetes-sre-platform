# 6. No NAT gateway in the capture VPC

Status: Accepted (2026-09-01)

## Context

The EKS cluster exists for evidence capture sessions of about four hours (ADR 0001). The
conventional VPC layout puts worker nodes in private subnets and gives them egress through
a NAT gateway per availability zone.

A NAT gateway is roughly $0.045/hr per AZ plus $0.045 per GB processed. Two AZs — which
correctness demands, since a single NAT gateway makes the surviving zone depend on the
failed one — is about $0.09/hr. That is comparable to the EKS control plane itself and a
substantial fraction of the whole session bill, for a cluster whose entire life is four
hours. Left running by accident it is around $65/month before a byte crosses it, which
would consume most of the sign-up credits funding this work.

## Decision

`infra/terraform/vpc.tf` sets `enable_nat_gateway = false` and
`map_public_ip_on_launch = true`. The EKS control-plane ENIs and the managed node group
both use `module.vpc.public_subnets`. The private subnets are still created, tagged
`kubernetes.io/role/internal-elb`, and left empty.

## Consequences

Worker nodes hold public IPv4 addresses and are directly reachable from the internet,
protected only by their security group. That is an acceptable exposure for a tagged,
throwaway cluster holding twelve seeded products and no credentials worth having, and it
is **not** what belongs in production. This record exists so nobody copies the pattern by
reading the Terraform and assuming it was considered production-shaped.

Public IPv4 addresses are themselves billed at about $0.005/hr each, so three nodes cost
around $0.015/hr — still roughly a third of one NAT gateway.

What production does instead is a two-line change, which is the reason the empty private
subnets are worth their zero cost: move the node group's `subnet_ids` in `eks.tf` to
`module.vpc.private_subnets`, and set `enable_nat_gateway = true` with
`single_nat_gateway = false` for per-AZ redundancy.

Interface VPC endpoints are the other production-adjacent option — ECR (`api` and `dkr`),
STS, EC2, ELB and CloudWatch Logs, plus an S3 gateway endpoint — and they remove most NAT
traffic while being cheaper at low volume. At about $0.01/hr each, seven of them cost more
than a NAT gateway over a four-hour cluster, so neither approach is used here. The
arithmetic, not the architecture, is what decided it.

Related cost decisions in the same file and its neighbours, for the same reason: EKS
control-plane logging is off (`enabled_cluster_log_types` empty, so no CloudWatch log
group is created), and the cluster is destroyed after every session with an ordered
teardown (`make eks-down` deletes LoadBalancer Services before `terraform destroy`, or the
NLB survives the VPC and blocks it).

## Rejected

**A conventional private-subnet VPC with per-AZ NAT.** The correct architecture. It roughly
doubles the hourly cost of a capture session and puts the largest single line item on a
resource that exists only so nodes can pull images they could pull directly.

**A single shared NAT gateway.** Half the cost of the correct version, and it makes the
`az-loss` scenario measure the wrong thing: if the surviving zone's egress ran through the
failed zone's NAT gateway, "what survives on redundancy alone" would have an answer
determined by the network topology rather than by the workload placement being tested.
