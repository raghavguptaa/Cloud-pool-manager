# Copy to terraform.tfvars and edit. Every value below is already the default; the file exists
# to show what is worth changing and what a second arm of the experiment looks like.
#
# terraform.tfvars is gitignored. example.tfvars is the one that is committed.

region      = "ap-south-1"
name_prefix = "cloud-pool-mgr"

# Named AWS CLI profile. Leave empty to use the default credential chain.
profile = ""

# "governor" sizes the warm pool from CPU every 60 s.
# "baseline" deploys the reactive arm instead: a CloudWatch alarm and a step scaling policy,
# with the same warm pool sized once at apply time and never touched again. Apply one, measure
# it, destroy it, then apply the other -- they are not meant to run side by side against the
# same group.
scaling_mode = "governor"

# Baseline arm only: the static pool it holds around the clock. Ignored in governor mode, where
# the controller owns these two numbers.
baseline_warm_pool_min_size              = 2
baseline_warm_pool_max_prepared_capacity = 3

instance_type = "t3.micro"

# The stand-in for application boot time, in seconds. This is the cost the warm pool removes
# from the scale-out path, so it is the single most interesting number to vary.
boot_delay_seconds = 180

# The original design spec is ambiguous about the scale-in threshold: its formula says 20, the
# narrative of the recorded run says 15. Set 15 here to deploy the other reading -- the
# controller reads this value from its environment, so no code changes.
scale_in_threshold = 20
pre_warm_threshold = 40

# Log the decision and write nothing. Worth one deploy before letting it move real capacity.
dry_run   = false
log_level = "INFO"

# Narrow this to your own address to run the experiment without a publicly reachable endpoint.
allowed_http_cidr = "0.0.0.0/0"

tags = {
  Project = "cloud-pool-manager"
}
