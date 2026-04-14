module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 6.7"

  name = var.cluster_name
  cidr = var.vpc_cidr
  azs  = local.azs

  # /20 per subnet, well beyond what the VPC CNI needs here. The offset of 8 on the private
  # range keeps the two families visually separable in the console.
  public_subnets  = [for i, _ in local.azs : cidrsubnet(var.vpc_cidr, 4, i)]
  private_subnets = [for i, _ in local.azs : cidrsubnet(var.vpc_cidr, 4, i + 8)]

  # ---------------------------------------------------------------------------------------
  # No NAT gateway. This is the single largest cost decision in the project and it is a
  # demo-only trade-off, not an architecture.
  #
  # A NAT gateway is about $0.045/hr per AZ plus $0.045 per GB processed. Two AZs for
  # correctness would be roughly $0.09/hr -- comparable to the EKS control plane itself and
  # a fifth of the entire session bill, for a cluster whose whole life is four hours. Left
  # running by accident it is about $65/month before a byte crosses it, which would consume
  # most of the sign-up credits funding this work.
  #
  # The cost of avoiding it: worker nodes sit in public subnets with public IPs, so every
  # node is directly addressable from the internet and is protected only by its security
  # group. That is an acceptable exposure for a tagged, throwaway cluster with no real data,
  # and it is NOT what belongs in production.
  #
  # In production: nodes go in the private subnets that this module already creates and
  # leaves empty, `enable_nat_gateway = true` and `single_nat_gateway = false` for per-AZ
  # redundancy, and node_group subnet_ids in eks.tf point at module.vpc.private_subnets. If
  # the only egress needed is to AWS APIs, interface VPC endpoints for ECR (api and dkr),
  # STS, EC2, ELB and CloudWatch Logs plus a gateway endpoint for S3 remove most of the NAT
  # traffic and are cheaper at low volume -- though at ~$0.01/hr each, seven endpoints cost
  # more than a NAT gateway for a four-hour cluster, which is why neither is used here.
  #
  # The private subnets are created despite being empty: they cost nothing, and they make
  # the production version a two-line change rather than a rewrite.
  # ---------------------------------------------------------------------------------------
  enable_nat_gateway = false

  # Required for the no-NAT arrangement: nodes need routable addresses to reach the EKS API,
  # ECR and the container registries. Note that public IPv4 addresses are themselves billed
  # (~$0.005/hr each) -- three nodes is about $0.015/hr, still a third of one NAT gateway.
  map_public_ip_on_launch = true

  # The in-cluster AWS resources (EBS volumes via the CSI driver) resolve regional endpoints
  # by DNS name, and EKS itself requires both of these.
  enable_dns_hostnames = true
  enable_dns_support   = true

  # Subnet discovery tags for the in-tree AWS cloud provider. Without the elb tag, a
  # Service type=LoadBalancer fails to place an NLB and the ingress never gets an address --
  # which during a capture session looks like an ingress-nginx problem for twenty minutes.
  public_subnet_tags = {
    "kubernetes.io/role/elb" = "1"
  }
  private_subnet_tags = {
    "kubernetes.io/role/internal-elb" = "1"
  }

  tags = local.tags
}
