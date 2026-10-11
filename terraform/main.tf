# Shared lookups and derived values. Everything that depends on which arm of the experiment
# is being deployed keys off local.is_governor, so the mode switch is expressed once.

locals {
  is_governor = var.scaling_mode == "governor"

  function_name = "${var.name_prefix}-governor"

  # Lambda writes to this log group whether or not anything creates it, so Terraform creates
  # it explicitly with a retention policy. See lambda.tf.
  log_group_name = "/aws/lambda/${var.name_prefix}-governor"

  # Both arms run a warm pool -- see the block in compute.tf for why. In baseline mode these are
  # static configuration that nothing ever changes. In governor mode they are initial conditions
  # the controller owns from its first invocation onward.
  warm_pool_min_size = local.is_governor ? var.warm_pool_initial_min_size : var.baseline_warm_pool_min_size

  warm_pool_max_prepared_capacity = local.is_governor ? var.warm_pool_initial_max_prepared_capacity : var.baseline_warm_pool_max_prepared_capacity

  user_data_vars = {
    boot_delay_seconds = var.boot_delay_seconds
    name_prefix        = var.name_prefix
  }
}

# The default VPC is used on purpose. A hand-rolled VPC would add a NAT gateway, route tables
# and subnet maths to a module whose subject is capacity control, and would cost more to run
# than everything else here combined. The one property this module actually needs from the
# network is that the load balancer can reach two availability zones, which the default VPC
# gives for free: it has one public subnet per AZ.
data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

# Resolved at plan time from the SSM public parameter rather than pinned to an AMI id, because
# an AMI id is region-specific and goes stale. The trade-off is that the fleet can change
# underneath a re-apply when Amazon publishes a new image; that is the right default for an
# experiment, and the wrong one for a fleet you need bit-identical across months.
data "aws_ssm_parameter" "al2023_ami" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

data "aws_caller_identity" "current" {}

data "aws_partition" "current" {}
