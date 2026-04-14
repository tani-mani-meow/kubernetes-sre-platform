provider "aws" {
  region = var.region

  # Tagging at the provider rather than only on the modules is the point of this block: the
  # modules below create resources this root module never names -- ENIs, security group
  # rules, the node group's autoscaling group, EBS volumes attached at boot. After a
  # destroy, the only practical way to find something that survived is to filter the console
  # by tag. Every resource here is disposable by construction, so anything still alive
  # afterwards is a mistake; these tags are what make it a findable one.
  default_tags {
    tags = local.tags
  }
}

provider "kubernetes" {
  host                   = module.eks.cluster_endpoint
  cluster_ca_certificate = base64decode(module.eks.cluster_certificate_authority_data)

  # exec, not a data.aws_eks_cluster_auth token. EKS tokens expire after 15 minutes, and an
  # apply that creates a node group takes longer than that, so a token resolved at plan time
  # is often already dead by the time the StorageClass is applied.
  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "aws"
    args        = ["eks", "get-token", "--cluster-name", module.eks.cluster_name, "--region", var.region]
  }
}
