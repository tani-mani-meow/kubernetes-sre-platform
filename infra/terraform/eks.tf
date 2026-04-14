module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 21.25"

  name               = var.cluster_name
  kubernetes_version = var.kubernetes_version

  vpc_id = module.vpc.vpc_id
  # Public subnets for both the control plane ENIs and the nodes -- see the no-NAT rationale
  # in vpc.tf.
  subnet_ids = module.vpc.public_subnets

  # kubectl comes from a laptop outside the VPC, so the public endpoint is the only way in.
  # Private access stays on as well so node-to-API traffic uses the in-VPC ENIs rather than
  # hairpinning out through the internet gateway.
  endpoint_public_access       = true
  endpoint_public_access_cidrs = var.cluster_endpoint_public_access_cidrs
  endpoint_private_access      = true

  # ---------------------------------------------------------------------------------------
  # Access: EKS Access Entries, not the aws-auth ConfigMap.
  #
  # "API" rather than "API_AND_CONFIG_MAP" because this cluster is created fresh with no
  # legacy tooling that writes aws-auth, so there is nothing to stay compatible with. In API
  # mode the ConfigMap is not consulted at all, which removes the failure this project cannot
  # afford: a malformed aws-auth edit is applied through the API server, cannot be reverted
  # without the access it just destroyed, and the only fix is destroy-and-recreate -- an hour
  # out of a four-hour session. Access entries live in the EKS control plane and are
  # repairable with the AWS CLI even after in-cluster access is gone.
  #
  # The trade-off worth stating: API mode means no ConfigMap fallback, so the access entry
  # below is the sole grant. That is why additional_cluster_admin_arns exists.
  #
  # This defaults to false in module v21 -- without it, terraform apply succeeds and then
  # kubectl returns "error: You must be logged in to the server (Unauthorized)".
  # ---------------------------------------------------------------------------------------
  authentication_mode                      = "API"
  enable_cluster_creator_admin_permissions = true

  # Keyed by ARN rather than a derived short name, so two roles with the same trailing path
  # segment cannot collide into one entry.
  access_entries = {
    for arn in var.additional_cluster_admin_arns : arn => {
      principal_arn = arn
      policy_associations = {
        admin = {
          policy_arn = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
          access_scope = {
            type = "cluster"
          }
        }
      }
    }
  }

  # Both default to on in module v21 (audit, api and authenticator, plus a log group). See
  # the variable description for why they are turned off here and what production would do.
  enabled_log_types           = var.enabled_cluster_log_types
  create_cloudwatch_log_group = length(var.enabled_cluster_log_types) > 0

  addons = {
    # before_compute puts the CNI and kube-proxy in place ahead of the node group, so nodes
    # come up Ready the first time instead of joining NotReady and waiting for a
    # reconciliation that adds several minutes to every cluster creation.
    vpc-cni = {
      before_compute = true
    }
    kube-proxy = {
      before_compute = true
    }

    # CoreDNS cannot be in that set -- its pods need somewhere to schedule, so it has to
    # follow the node group rather than precede it.
    coredns = {}

    # The one mandatory IRSA in this project. Without a credentialed EBS CSI controller the
    # Prometheus, Loki and Tempo PVCs stay Pending, and the entire observability stack -- and
    # therefore every SLI, alert and chaos measurement -- never starts.
    aws-ebs-csi-driver = {
      service_account_role_arn = module.ebs_csi_irsa.arn
    }
  }

  eks_managed_node_groups = {
    default = {
      instance_types = var.node_instance_types
      capacity_type  = var.node_capacity_type
      ami_type       = "AL2023_x86_64_STANDARD"

      min_size     = var.node_min_size
      max_size     = var.node_max_size
      desired_size = var.node_desired_size

      # Spanning both AZs in one node group is what makes the AZ-loss scenario possible:
      # cordon and drain every node carrying one topology.kubernetes.io/zone value and the
      # survivors in the other zone have to absorb the load.
      subnet_ids = module.vpc.public_subnets

      # block_device_mappings rather than disk_size. The module creates a custom launch
      # template by default, and in that path it explicitly nulls disk_size -- setting it
      # would be silently ignored and nodes would come up with the 20 GiB default.
      block_device_mappings = {
        root = {
          device_name = "/dev/xvda"
          ebs = {
            volume_size           = var.node_disk_size
            volume_type           = "gp3"
            encrypted             = true
            delete_on_termination = true
          }
        }
      }
    }
  }

  tags = local.tags
}

# ------------------------------------------------------------------------------------------
# IRSA role for the EBS CSI controller.
#
# Pod Identity is now the simpler alternative for this (the module's addons block accepts a
# pod_identity_association), but IRSA is used deliberately: it is what the existing portfolio
# repos and most running clusters use, and the OIDC-provider trust relationship is the thing
# worth being able to explain in an interview.
#
# NOTE: this submodule is `iam-role-for-service-accounts` in terraform-aws-iam v6.x. It was
# named `iam-role-for-service-accounts-eks` through v5.x, and most published examples still
# use the old path, which no longer exists.
# ------------------------------------------------------------------------------------------
module "ebs_csi_irsa" {
  source  = "terraform-aws-modules/iam/aws//modules/iam-role-for-service-accounts"
  version = "~> 6.8"

  name = "${var.cluster_name}-ebs-csi"

  attach_ebs_csi_policy = true

  oidc_providers = {
    main = {
      provider_arn = module.eks.oidc_provider_arn
      # Must match the service account the managed addon actually creates. A typo here
      # produces volumes stuck in Pending with an AccessDenied buried in the controller log.
      namespace_service_accounts = ["kube-system:ebs-csi-controller-sa"]
    }
  }

  tags = local.tags
}
