# Shared locals and lookups. Resources live in vpc.tf, eks.tf and storage.tf.

data "aws_caller_identity" "current" {}

data "aws_availability_zones" "available" {
  state = "available"

  # Local Zones and Wavelength zones are returned by this data source but are not valid for
  # EKS subnets, and picking one produces an unhelpful error several minutes into an apply.
  filter {
    name   = "opt-in-status"
    values = ["opt-in-not-required"]
  }
}

locals {
  azs = slice(data.aws_availability_zones.available.names, 0, var.az_count)

  tags = merge(
    {
      # Project matches the app.kubernetes.io/part-of label in docs/conventions.md, so a
      # console tag filter and a kubectl label selector return the same world.
      Project = var.cluster_name

      # Ephemeral is the tag that matters after teardown. Anything found in the console
      # carrying Ephemeral=true is by definition something that should no longer exist.
      Ephemeral = "true"

      ManagedBy = "terraform"
      Purpose   = "evidence-capture"
    },
    var.extra_tags,
  )
}
