output "cluster_name" {
  description = "EKS cluster name."
  value       = module.eks.cluster_name
}

output "cluster_endpoint" {
  description = "EKS API server endpoint."
  value       = module.eks.cluster_endpoint
}

output "region" {
  description = "AWS region the cluster was created in."
  value       = var.region
}

output "update_kubeconfig_command" {
  description = "Ready to paste. First command of every capture session."
  value       = "aws eks update-kubeconfig --region ${var.region} --name ${module.eks.cluster_name}"
}

output "availability_zones" {
  description = <<-EOT
    The AZs the node group spans. The AZ-loss chaos scenario needs these by name: it selects
    nodes with `kubectl get nodes -l topology.kubernetes.io/zone=<az>` and cordons and drains
    them all. Reading them from Terraform rather than hardcoding keeps the script working if
    the region or az_count changes.
  EOT
  value       = local.azs
}

output "node_group_name" {
  description = <<-EOT
    Managed node group name, for the node-drain scenario and console lookups. Taken from
    node_group_id (formatted `cluster:nodegroup`) rather than the Terraform map key, because
    the module may prefix the real AWS name.
  EOT
  value       = split(":", module.eks.eks_managed_node_groups["default"].node_group_id)[1]
}

output "vpc_id" {
  description = "VPC id. Needed for the post-destroy orphan sweep in README.md."
  value       = module.vpc.vpc_id
}

output "ebs_csi_irsa_role_arn" {
  description = "IAM role assumed by the EBS CSI controller. Check this first when PVCs stay Pending."
  value       = module.ebs_csi_irsa.arn
}

output "nlb_hostname_lookup" {
  description = <<-EOT
    How to find the ingress address once ingress-nginx is deployed. The NLB is created by the
    in-cluster cloud provider in response to the Service, not by Terraform, so it has no
    Terraform output and does not appear in state -- which is also why it is the resource most
    likely to be left behind. See the teardown order in README.md.
  EOT
  value       = "kubectl -n ingress-nginx get svc ingress-nginx-controller -o jsonpath='{.status.loadBalancer.ingress[0].hostname}'"
}

output "orphan_sweep_commands" {
  description = <<-EOT
    Run both after `terraform destroy`; both should return nothing.

    Two commands rather than one because there are two populations of resources with two
    different tags. Terraform's default_tags cover only what Terraform created. Anything the
    in-cluster AWS cloud provider created on the cluster's behalf -- the ingress NLB, its
    security group, EBS volumes backing PVCs -- never passed through Terraform and is tagged
    `kubernetes.io/cluster/<name> = owned` instead. Sweeping only on Project is the mistake
    that leaves an NLB billing.
  EOT
  value = {
    terraform_owned = "aws resourcegroupstaggingapi get-resources --region ${var.region} --tag-filters Key=Project,Values=${var.cluster_name} --query 'ResourceTagMappingList[].ResourceARN' --output table"
    cluster_owned   = "aws resourcegroupstaggingapi get-resources --region ${var.region} --tag-filters Key=kubernetes.io/cluster/${module.eks.cluster_name} --query 'ResourceTagMappingList[].ResourceARN' --output table"
  }
}
