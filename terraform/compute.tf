# The fleet, the warm pool, and -- in baseline mode -- the reactive scaling it is measured
# against.

# The fleet holds no application credentials. This role exists only so Systems Manager Session
# Manager can open a shell on an instance for debugging, which keeps port 22 closed and means
# there is no key pair to manage, rotate or leak. It is also the difference between being able
# to inspect a warm pool instance and guessing at what it did.
data "aws_iam_policy_document" "instance_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "instance" {
  name               = "${var.name_prefix}-instance"
  description        = "Session Manager access for the application fleet"
  assume_role_policy = data.aws_iam_policy_document.instance_assume_role.json
}

resource "aws_iam_role_policy_attachment" "instance_ssm" {
  role       = aws_iam_role.instance.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "instance" {
  name = "${var.name_prefix}-instance"
  role = aws_iam_role.instance.name
}

resource "aws_launch_template" "app" {
  name_prefix   = "${var.name_prefix}-lt-"
  description   = "Application fleet for the warm pool experiment"
  image_id      = data.aws_ssm_parameter.al2023_ami.value
  instance_type = var.instance_type

  user_data = base64encode(templatefile("${path.module}/user_data.sh.tftpl", local.user_data_vars))

  vpc_security_group_ids = [aws_security_group.instance.id]

  # Detailed, 1-minute instance monitoring. This one line is load-bearing for the entire
  # result. At the default 5-minute resolution CloudWatch publishes three datapoints across a
  # whole spike-and-recovery, so a controller polling every 60 seconds would spend most of its
  # invocations re-reading the same stale average and could not see a micro-burst at all.
  #
  # It is deliberately off in baseline mode. The reactive arm's alarm latency is dominated by
  # that 5-minute publish interval, and quietly enabling detailed monitoring there would
  # improve the arm being measured against and erase the difference under test.
  #
  # Cost: seven metrics per instance at $0.30 per metric per month, so roughly $2.10 per
  # instance per month on top of the instance itself.
  monitoring {
    enabled = local.is_governor
  }

  # Encrypted at rest with the account's default EBS key. Warm pool instances are stopped, not
  # terminated, so these volumes persist -- and are billed -- for as long as the pool is warm.
  block_device_mappings {
    device_name = "/dev/xvda"

    ebs {
      volume_size           = var.root_volume_size
      volume_type           = "gp3"
      encrypted             = true
      delete_on_termination = true
    }
  }

  # IMDSv2 only. The instances run a load-generator target reachable from the internet through
  # the load balancer, and IMDSv1's unauthenticated GET is the classic path from an SSRF bug to
  # the instance role's credentials.
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
    instance_metadata_tags      = "enabled"
  }

  iam_instance_profile {
    name = aws_iam_instance_profile.instance.name
  }

  tag_specifications {
    resource_type = "instance"

    tags = merge(var.tags, {
      Name = "${var.name_prefix}-app"
    })
  }

  tag_specifications {
    resource_type = "volume"

    tags = merge(var.tags, {
      Name = "${var.name_prefix}-app"
    })
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_autoscaling_group" "app" {
  name                = "${var.name_prefix}-asg"
  vpc_zone_identifier = data.aws_subnets.default.ids
  min_size            = var.asg_min_size
  max_size            = var.asg_max_size
  desired_capacity    = var.asg_initial_desired_capacity
  target_group_arns   = [aws_lb_target_group.app.arn]

  # Health is judged by the load balancer, not just by the hypervisor. An instance that is
  # running but has not finished booting the application is not useful capacity, and the whole
  # question here is how long that gap lasts.
  health_check_type = "ELB"

  # The grace period has to outlast the simulated boot delay, or the group terminates and
  # replaces every instance it launches, forever.
  health_check_grace_period = var.boot_delay_seconds + 120

  launch_template {
    id      = aws_launch_template.app.id
    version = "$Latest"
  }

  # Warm pool instances are launched, allowed to run user data once, then stopped. Restarting a
  # stopped instance skips the whole first-boot path -- that is the entire trick: the boot cost
  # is paid in advance, off the critical path, and a scale-out becomes a start rather than a
  # launch.
  #
  # BOTH arms get one, because both arms had one in the run this reproduces: the recorded
  # baseline timeline in data/baseline_events.csv extracts a pooled instance 6 s after its alarm
  # fires and has it in service 1 s later. Denying the reactive arm a warm pool would add ~180 s
  # of cold boot to the thing being compared against and credit the controller with a difference
  # the measurement never contained.
  #
  # What actually differs between the arms is who sizes the pool. Here it is fixed at apply time
  # and nothing ever moves it -- the "static warm pool" scenario finops/ prices, which pays for
  # stopped instances' EBS around the clock. In governor mode the controller rewrites these two
  # numbers every 60 s, which is the capability AWS has no native equivalent for: scaling
  # policies move DesiredCapacity, never the pool's own bounds.
  warm_pool {
    pool_state                  = "Stopped"
    min_size                    = local.warm_pool_min_size
    max_group_prepared_capacity = local.warm_pool_max_prepared_capacity

    # On scale-in, return the instance to the pool instead of terminating it. Without this
    # the pool refills by launching from scratch and pays the boot cost it exists to avoid.
    instance_reuse_policy {
      reuse_on_scale_in = true
    }
  }

  # Auto Scaling group tags are not covered by the provider's default_tags, so they are
  # propagated explicitly.
  dynamic "tag" {
    for_each = merge(var.tags, { Name = "${var.name_prefix}-app" })

    content {
      key                 = tag.key
      value               = tag.value
      propagate_at_launch = true
    }
  }

  # Both arms hand capacity to something outside Terraform: the controller calls
  # UpdateAutoScalingGroup and PutWarmPool every 60 seconds, and the step scaling policy moves
  # desired capacity on its own. Without ignore_changes the next apply -- or a CI drift check --
  # would reset whatever the running system had just decided, and the two would fight for as
  # long as anyone kept applying. The values above are initial conditions, not desired state.
  #
  # The cost of that: editing the warm pool variables after the first apply has no effect,
  # because this block ignores the drift it would show up as. Re-apply with -replace on the
  # group, or call PutWarmPool directly, if you need to resize a pool that is already running.
  lifecycle {
    ignore_changes = [desired_capacity, warm_pool]
  }
}

# --------------------------------------------------------------------------------------------
# Reactive arm (scaling_mode = "baseline")
#
# A CloudWatch alarm on the group's average CPU driving a step scaling policy. This is the
# standard answer to "scale on load", and reproducing it faithfully is the point: the latency
# it exhibits is not a misconfiguration, it is what the mechanism costs. Basic monitoring
# publishes every 5 minutes, so the alarm cannot observe a spike sooner than the next publish,
# and the instance it then launches still has to boot.
# --------------------------------------------------------------------------------------------

resource "aws_autoscaling_policy" "baseline_scale_out" {
  count = local.is_governor ? 0 : 1

  name                   = "${var.name_prefix}-scale-out"
  autoscaling_group_name = aws_autoscaling_group.app.name
  policy_type            = "StepScaling"
  adjustment_type        = "ChangeInCapacity"

  # Step scaling uses the warmup instead of a cooldown: an instance that is still booting is
  # not counted towards the group's capacity, so the policy does not add a second instance
  # while the first one is on its way.
  estimated_instance_warmup = var.boot_delay_seconds

  # Matches the alarm's statistic below. A mismatch here is a quiet way to have the policy
  # evaluate a different number from the one that triggered it.
  metric_aggregation_type = "Average"

  step_adjustment {
    scaling_adjustment          = 1
    metric_interval_lower_bound = 0
  }
}

resource "aws_cloudwatch_metric_alarm" "baseline_cpu_high" {
  count = local.is_governor ? 0 : 1

  alarm_name          = "${var.name_prefix}-cpu-high"
  alarm_description   = "Reactive arm: scale out when group CPU crosses the pre-warm threshold"
  namespace           = "AWS/EC2"
  metric_name         = "CPUUtilization"
  statistic           = "Average"
  period              = var.baseline_alarm_period
  evaluation_periods  = var.baseline_alarm_evaluation_periods
  threshold           = var.pre_warm_threshold
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_autoscaling_policy.baseline_scale_out[0].arn]

  dimensions = {
    AutoScalingGroupName = aws_autoscaling_group.app.name
  }
}
