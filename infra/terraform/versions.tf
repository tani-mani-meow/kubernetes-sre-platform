terraform {
  # 1.5.7 is the floor imposed by the EKS and IAM modules, not a preference.
  required_version = ">= 1.5.7"

  required_providers {
    aws = {
      source = "hashicorp/aws"
      # EKS module v21 requires >= 6.59 (access entries, addon namespace_config). Pinned to
      # the 6.x minor so a provider major bump cannot land silently between two capture
      # sessions and turn a 4.5-hour budget into an afternoon of debugging a plan diff.
      version = "~> 6.62"
    }
    kubernetes = {
      source = "hashicorp/kubernetes"
      # Only used for the gp3 StorageClass and the gp2 default-class removal below.
      version = "~> 3.2"
    }
  }

  # -------------------------------------------------------------------------------------
  # State backend: deliberately local. No `backend "s3"` block.
  #
  # This cluster is created and destroyed by one person on one machine, and lives about
  # four hours. An S3 backend would need its own bucket (and its lock configuration)
  # bootstrapped first -- a permanent AWS footprint, created to protect something designed
  # to be impermanent, and one more thing to remember to remove. Remote state earns its
  # keep with concurrent operators or CI; there are neither here.
  #
  # The failure mode is real and worth naming rather than hiding: if this state file is
  # lost while a cluster is running, `terraform destroy` can no longer reach the
  # resources, and the cluster has to be dismantled by hand through the console -- EKS
  # control plane, node group, launch template, VPC, IGW, security groups and lingering
  # ENIs -- while the control plane bills $0.10/hr and any leaked NLB bills alongside it.
  #
  # Two things make that recoverable rather than fatal:
  #   1. Every resource carries the Project tag set in providers.tf, so Resource Groups /
  #      Tag Editor gives a complete inventory to delete from, in one filtered view.
  #   2. This repository lives under OneDrive, so terraform.tfstate is replicated off the
  #      machine continuously without any extra tooling. That is a backup, not a lock --
  #      it works precisely because only one machine ever writes it.
  #
  # terraform.tfstate is gitignored at the repo root. It contains the cluster CA data and
  # must stay out of a public repo.
  # -------------------------------------------------------------------------------------
}
