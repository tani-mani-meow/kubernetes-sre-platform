# ------------------------------------------------------------------------------------------
# Default StorageClass.
#
# The EBS CSI driver addon installs no StorageClass of its own. EKS ships a built-in `gp2`
# class already marked default, so without the two resources below every PVC in the
# observability stack silently lands on gp2: older technology, 3 IOPS/GB (a 10 GiB Prometheus
# volume gets 30 IOPS and 128 MiB/s of burst), and about 25% more per GiB than gp3, which
# includes 3000 IOPS and 125 MB/s at no extra charge. Losing that comparison by default is a
# bad trade on a cluster whose job is to stay responsive while being deliberately abused.
#
# These are the only Kubernetes objects Terraform owns. Everything else in the cluster is
# Argo CD's, per the GitOps decision in planning.md -- a StorageClass is infrastructure
# because it is a property of the machine the cluster runs on, and it has to exist before the
# first PVC is reconciled.
# ------------------------------------------------------------------------------------------

resource "kubernetes_storage_class_v1" "gp3" {
  metadata {
    name = "gp3"
    annotations = {
      "storageclass.kubernetes.io/is-default-class" = "true"
    }
  }

  storage_provisioner = "ebs.csi.aws.com"
  reclaim_policy      = "Delete"

  # WaitForFirstConsumer is load-bearing with two AZs, not a default worth copying blindly.
  # Immediate binding provisions the EBS volume before the pod is scheduled, so the volume
  # can land in us-east-1a while the scheduler puts the pod in us-east-1b, and the pod stays
  # Pending forever on a volume-node affinity conflict. That failure would surface during the
  # AZ-loss experiment -- exactly when it is least distinguishable from the thing being
  # measured.
  volume_binding_mode = "WaitForFirstConsumer"

  # Prometheus and Loki are the two most likely to be resized mid-session once real retention
  # is known; without this, growing a volume means deleting the PVC and the data with it.
  allow_volume_expansion = true

  parameters = {
    type   = "gp3"
    fsType = "ext4"
    # Encryption at rest costs nothing on EBS and removes a question from any security
    # review of the repo. The default AWS-managed key is used, so there is no KMS key to
    # leak past teardown.
    encrypted = "true"
  }

  depends_on = [module.eks]
}

# A cluster with two default StorageClasses has undefined behaviour for a PVC that names
# neither, so gp2 has to stop being default for gp3 to reliably win. gp2 is created by EKS
# and is not a Terraform resource, hence a server-side-apply annotation patch rather than an
# ordinary resource. force is required because the annotation's current field manager is the
# EKS control plane.
resource "kubernetes_annotations" "gp2_not_default" {
  api_version = "storage.k8s.io/v1"
  kind        = "StorageClass"
  force       = true

  metadata {
    name = "gp2"
  }

  annotations = {
    "storageclass.kubernetes.io/is-default-class" = "false"
  }

  depends_on = [module.eks]
}
