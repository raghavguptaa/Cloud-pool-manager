# Everything that a live re-run might want to change lives here, so re-running the
# experiment against a different region, instance size or threshold pair is a .tfvars edit
# rather than a diff against the configuration itself.

variable "region" {
  description = "AWS region to deploy into. The captured telemetry came from ap-south-1."
  type        = string
  default     = "ap-south-1"
}

variable "profile" {
  description = "Named AWS CLI profile. Leave empty to use the default credential chain."
  type        = string
  default     = ""
}

variable "name_prefix" {
  description = "Prefix for every resource name. Keep it short: load balancer and target group names are capped at 32 characters."
  type        = string
  default     = "cloud-pool-mgr"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,19}$", var.name_prefix))
    error_message = "name_prefix must be 2-20 lowercase alphanumeric or hyphen characters and start with a letter."
  }
}

variable "scaling_mode" {
  type    = string
  default = "governor"

  description = <<-EOT
    Which arm of the experiment to deploy.

      baseline - reactive scaling only: a CloudWatch alarm on 5-minute basic-monitoring
                 metrics driving a step scaling policy, no warm pool and no controller.
      governor - the warm pool plus the Lambda controller on a 1-minute schedule, and no
                 step scaling policy at all.

    The two are mutually exclusive on purpose. Running both against one Auto Scaling group
    would let the step policy and the controller issue conflicting capacity writes, and the
    measurement would be of the argument between them rather than of either strategy.
  EOT

  validation {
    condition     = contains(["baseline", "governor"], var.scaling_mode)
    error_message = "scaling_mode must be either \"baseline\" or \"governor\"."
  }
}

variable "instance_type" {
  description = "Instance type for the application fleet."
  type        = string
  default     = "t3.micro"
}

variable "root_volume_size" {
  description = "Root EBS volume size in GiB. Warm pool instances are stopped, not terminated, so this volume is billed for the whole time they sit in the pool."
  type        = number
  default     = 8
}

variable "boot_delay_seconds" {
  description = "Artificial delay in user data before the web server starts, standing in for real application boot time. This is the cost the warm pool exists to hide."
  type        = number
  default     = 180
}

variable "asg_min_size" {
  description = "Auto Scaling group minimum size."
  type        = number
  default     = 1
}

variable "asg_max_size" {
  description = "Auto Scaling group maximum size. Must be at least as large as the highest max_group_prepared_capacity the controller will write (4 in the mitigate phase)."
  type        = number
  default     = 4
}

variable "asg_initial_desired_capacity" {
  description = "Desired capacity at apply time. In governor mode the controller owns this value from its first invocation onward; see the lifecycle block in compute.tf."
  type        = number
  default     = 1
}

variable "warm_pool_initial_min_size" {
  description = "Governor arm's warm pool minimum size at apply time. The controller owns this value from its first invocation onward. Defaults to the cost-saving phase value, which is where an idle deployment starts."
  type        = number
  default     = 0
}

variable "warm_pool_initial_max_prepared_capacity" {
  description = "Governor arm's warm pool max_group_prepared_capacity at apply time. The controller owns this value thereafter. Defaults to the cost-saving phase value."
  type        = number
  default     = 1
}

variable "baseline_warm_pool_min_size" {
  description = "Baseline arm's warm pool minimum size. Static: nothing resizes it at runtime, which is the whole difference between the arms. Defaults to the same pool the governor holds only transiently during PRE_WARM, so the two are comparable."
  type        = number
  default     = 2
}

variable "baseline_warm_pool_max_prepared_capacity" {
  description = "Baseline arm's warm pool max_group_prepared_capacity. Static, like the minimum above."
  type        = number
  default     = 3
}

variable "scale_in_threshold" {
  description = "CPU percentage below which the controller drains the warm pool. The original design spec is ambiguous here -- its formula says 20, its run narrative says 15 -- so both are reachable from configuration."
  type        = number
  default     = 20

  validation {
    condition     = var.scale_in_threshold >= 0 && var.scale_in_threshold <= 100
    error_message = "scale_in_threshold must be a CPU percentage between 0 and 100."
  }
}

variable "pre_warm_threshold" {
  description = "CPU percentage at or above which the controller pulls warm instances into service."
  type        = number
  default     = 40

  validation {
    condition     = var.pre_warm_threshold > var.scale_in_threshold && var.pre_warm_threshold <= 100
    error_message = "pre_warm_threshold must be greater than scale_in_threshold and no greater than 100."
  }
}

variable "metric_period" {
  description = "Period in seconds the controller requests from CloudWatch. 60 requires detailed monitoring on the instances."
  type        = number
  default     = 60

  validation {
    condition     = contains([60, 300], var.metric_period)
    error_message = "metric_period must be 60 (detailed monitoring) or 300 (basic monitoring)."
  }
}

variable "baseline_alarm_period" {
  description = "Period in seconds for the reactive arm's CloudWatch alarm. 300 is not a tuning choice: basic monitoring only publishes every 5 minutes, so a shorter period would spend most of its evaluations on missing data."
  type        = number
  default     = 300
}

variable "baseline_alarm_evaluation_periods" {
  description = "Consecutive breaching periods before the reactive arm's alarm fires. One period is the most aggressive setting available, which is what makes the measured alarm latency a floor rather than a tuning artifact."
  type        = number
  default     = 1
}

variable "dry_run" {
  description = "When true the controller logs the capacity decision it would have made and writes nothing. Useful for a first deploy against a live Auto Scaling group."
  type        = bool
  default     = false
}

variable "log_level" {
  description = "Python log level for the controller."
  type        = string
  default     = "INFO"

  validation {
    condition     = contains(["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], var.log_level)
    error_message = "log_level must be a standard Python log level name."
  }
}

variable "log_retention_days" {
  description = "Retention for the controller's log group. An unset retention means the logs are kept forever, which is a slow and easily missed cost leak."
  type        = number
  default     = 14

  validation {
    condition     = contains([1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1096, 1827, 2192, 2557, 2922, 3288, 3653], var.log_retention_days)
    error_message = "log_retention_days must be one of the retention values CloudWatch Logs accepts."
  }
}

variable "enable_deletion_protection" {
  description = "Deletion protection on the load balancer. Off by default because this deployment is meant to be destroyed; turn it on for anything that outlives an afternoon."
  type        = bool
  default     = false
}

variable "allowed_http_cidr" {
  description = "CIDR allowed to reach the load balancer on port 80. Narrow this to your own address to run the experiment without exposing the endpoint to the internet."
  type        = string
  default     = "0.0.0.0/0"
}

variable "tags" {
  description = "Tags applied to every resource that supports default tags, and propagated to instances."
  type        = map(string)

  default = {
    Project = "cloud-pool-manager"
  }
}
