variable "region" {
  description = "AWS region for the evidence-capture cluster."
  type        = string
  default     = "us-east-1"
}

variable "cluster_name" {
  description = "EKS cluster name. Also used as the VPC name and the base for IAM role names."
  type        = string
  default     = "k8s-slo-platform"
}

variable "kubernetes_version" {
  description = <<-EOT
    EKS control plane minor version. Must match the kind node image minor in
    infra/kind/kind-config.yaml -- the two environments are only comparable if they run the
    same Kubernetes API surface.
  EOT
  type        = string
  default     = "1.34"
}

variable "az_count" {
  description = <<-EOT
    Number of availability zones. Two is both the EKS minimum and exactly what the AZ-loss
    chaos scenario needs: with one AZ there is nowhere to fail over to, and with three the
    experiment stops being interesting because losing a third of capacity barely registers.
  EOT
  type        = number
  default     = 2

  validation {
    condition     = var.az_count >= 2
    error_message = "EKS requires subnets in at least two availability zones, and the AZ-loss scenario is meaningless below two."
  }
}

variable "vpc_cidr" {
  description = "VPC CIDR. Sized for far more than this cluster needs; address space is free."
  type        = string
  default     = "10.0.0.0/16"
}

variable "node_instance_types" {
  description = <<-EOT
    Instance types for the managed node group. t3.large (2 vCPU / 8 GiB) is the smallest
    size that fits kube-prometheus-stack, Loki, Tempo, the OTel collector, Argo CD, Argo
    Rollouts, Chaos Mesh, ingress-nginx, three app services and Postgres with enough
    headroom left that a drained node's pods can actually reschedule onto the survivors.
    Smaller nodes turn every chaos experiment into a study of Pending pods.
  EOT
  type        = list(string)
  default     = ["t3.large"]
}

variable "node_capacity_type" {
  description = <<-EOT
    ON_DEMAND or SPOT.

    Defaulted to ON_DEMAND on purpose, against the usual demo-cluster instinct. The
    deliverable of this project is measured recovery time from deliberate, controlled
    failures. A spot reclamation is an uncontrolled node termination arriving mid-experiment,
    and once one happens there is no way to tell whether the MTTR just recorded belongs to
    the chaos experiment or to AWS taking capacity back -- the evidence is silently
    worthless, which is worse than expensive.

    Spot would save roughly $0.15/hr, about $0.68 across a 4.5-hour session, on a session
    that costs under two dollars. That is not a trade worth making against a scarce,
    non-repeatable block of capture time.
  EOT
  type        = string
  default     = "ON_DEMAND"

  validation {
    condition     = contains(["ON_DEMAND", "SPOT"], var.node_capacity_type)
    error_message = "node_capacity_type must be ON_DEMAND or SPOT."
  }
}

variable "node_min_size" {
  description = "Minimum node count."
  type        = number
  default     = 2
}

variable "node_max_size" {
  description = <<-EOT
    Maximum node count. One above desired, so a rolling node replacement has room to bring
    up a new node before removing the old one. Not an autoscaling target -- no Cluster
    Autoscaler or Karpenter is installed, and adding one would let the cluster grow the bill
    on its own during an unattended session.
  EOT
  type        = number
  default     = 4
}

variable "node_desired_size" {
  description = <<-EOT
    Desired node count. Three across two AZs means one AZ holds two nodes and the other
    holds one, so the AZ-loss scenario has an asymmetric failure to measure rather than a
    tidy half-and-half split.
  EOT
  type        = number
  default     = 3
}

variable "node_disk_size" {
  description = <<-EOT
    Root volume size in GiB per node. The EKS default of 20 GiB is not enough once the
    observability stack images are pulled onto every node; a node that hits DiskPressure
    starts evicting pods, which looks exactly like a chaos experiment that was never run.
  EOT
  type        = number
  default     = 50
}

variable "enabled_cluster_log_types" {
  description = <<-EOT
    EKS control plane log types shipped to CloudWatch. Empty by default, which is a
    deliberate choice rather than an oversight.

    Nothing in the evidence pipeline reads control plane logs: the SLIs, burn-rate alerts,
    canary analysis and chaos timings all come from in-cluster Prometheus, Loki and Tempo.
    The audit log in particular is by far the chattiest of the five and is billed on
    ingestion, so enabling it would add cost proportional to how hard the cluster is being
    driven -- and driving the cluster hard is the entire point of a capture session. A
    CloudWatch log group also outlives `terraform destroy` when the module did not create it,
    making it a classic post-teardown leftover.

    In production this would be the opposite default: audit and authenticator at minimum,
    with a retention policy, because there they are the only record of who did what to the
    control plane. Set this to ["audit", "authenticator"] for a session that needs it.

    Valid values: api, audit, authenticator, controllerManager, scheduler.
  EOT
  type        = list(string)
  default     = []
}

variable "cluster_endpoint_public_access_cidrs" {
  description = <<-EOT
    CIDRs allowed to reach the public EKS API endpoint. The endpoint has to be public: there
    is no NAT gateway, no VPN and no bastion, so kubectl from the author's laptop is the only
    path in.

    0.0.0.0/0 is the default because a home connection's address changes and a capture
    session locked out by a DHCP renewal is a wasted session. Narrowing this to the current
    address (curl ifconfig.me, append /32) is strictly better whenever the session can afford
    the risk, and is the right setting for anything that is not a four-hour throwaway.
  EOT
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "additional_cluster_admin_arns" {
  description = <<-EOT
    Extra IAM principal ARNs to grant cluster-admin via EKS access entries, on top of the
    identity that runs terraform apply. Normally empty. Exists because losing admin access to
    a cluster whose only recovery is to destroy and recreate it costs a chunk of a capture
    session -- an assumed-role ARN alongside a user ARN is cheap insurance.
  EOT
  type        = list(string)
  default     = []
}

variable "extra_tags" {
  description = "Additional tags merged into the tags applied to every resource."
  type        = map(string)
  default     = {}
}
