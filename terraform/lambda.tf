# The controller: a Python function that reads one CloudWatch metric, runs the decision
# function in governor/fsm.py, and writes the three Auto Scaling knobs. Deployed only in
# governor mode -- in baseline mode the step scaling policy in compute.tf plays its part, and
# none of these resources exist.

# Packaged straight from the repository's governor/ directory, so the deployed artifact is the
# same code the test suite runs against and there is no build step to drift.
#
# Explicit source blocks rather than source_dir: source_dir would put config.py and handler.py
# at the root of the archive, and the handler is governor.handler.lambda_handler -- the module
# has to stay inside a governor/ package directory in the zip. The "*.py" pattern also keeps
# __pycache__ out of the artifact, which matters because a stale .pyc changes the hash and
# triggers a pointless redeploy.
#
# No dependencies are vendored: boto3 is already in the Lambda Python runtime, and the
# controller imports nothing else.
data "archive_file" "governor" {
  type        = "zip"
  output_path = "${path.module}/build/governor.zip"

  dynamic "source" {
    for_each = fileset("${path.module}/../governor", "*.py")

    content {
      content  = file("${path.module}/../governor/${source.value}")
      filename = "governor/${source.value}"
    }
  }
}

data "aws_iam_policy_document" "governor_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

# Exactly the five API calls the controller makes, split by what AWS will let us scope.
data "aws_iam_policy_document" "governor" {
  # checkov:skip=CKV_AWS_356:The two describe actions and GetMetricData do not accept a
  # resource ARN; see the comment on the statement below.
  statement {
    sid    = "WriteCapacityForThisGroupOnly"
    effect = "Allow"

    actions = [
      "autoscaling:UpdateAutoScalingGroup",
      "autoscaling:PutWarmPool",
    ]

    resources = [aws_autoscaling_group.app.arn]
  }

  # These three are resource-level-permission-free on the AWS side, not by choice here.
  # cloudwatch:GetMetricData is documented with no resource types at all, and every
  # autoscaling:Describe* action ignores the resource element -- an ARN in it denies the call
  # outright rather than narrowing it. The blast radius is a read of metric data and of Auto
  # Scaling group descriptions; the two actions that can change anything are scoped above.
  statement {
    sid    = "ReadOnlyActionsWithNoResourceLevelSupport"
    effect = "Allow"

    actions = [
      "cloudwatch:GetMetricData",
      "autoscaling:DescribeAutoScalingGroups",
      "autoscaling:DescribeWarmPool",
    ]

    resources = ["*"]
  }
}

# Logging is granted separately and deliberately narrowly. The usual move is to attach the
# AWSLambdaBasicExecutionRole managed policy, which also grants logs:CreateLogGroup on every
# log group in the account. Terraform owns the log group below, so the function never needs to
# create one, and these two actions on one group are the whole requirement.
data "aws_iam_policy_document" "governor_logs" {
  statement {
    sid    = "WriteOwnLogStreams"
    effect = "Allow"

    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]

    resources = [
      "arn:${data.aws_partition.current.partition}:logs:${var.region}:${data.aws_caller_identity.current.account_id}:log-group:${local.log_group_name}:log-stream:*",
    ]
  }
}

resource "aws_iam_role" "governor" {
  count = local.is_governor ? 1 : 0

  name               = "${var.name_prefix}-governor"
  description        = "Execution role for the warm pool controller"
  assume_role_policy = data.aws_iam_policy_document.governor_assume_role.json
}

resource "aws_iam_role_policy" "governor" {
  count = local.is_governor ? 1 : 0

  name   = "capacity-control"
  role   = aws_iam_role.governor[0].id
  policy = data.aws_iam_policy_document.governor.json
}

resource "aws_iam_role_policy" "governor_logs" {
  count = local.is_governor ? 1 : 0

  name   = "own-log-streams"
  role   = aws_iam_role.governor[0].id
  policy = data.aws_iam_policy_document.governor_logs.json
}

# Created here rather than left to Lambda's implicit creation, which produces a group with no
# retention policy. Logs kept forever are a cost leak that nobody notices for a year, and this
# function writes a line every 60 seconds.
resource "aws_cloudwatch_log_group" "governor" {
  # checkov:skip=CKV_AWS_338:One year of retention is for audit trails. These logs are
  # operational output from an experiment and are worth keeping for a fortnight.
  # checkov:skip=CKV_AWS_158:Encrypting with a customer-managed key adds a KMS key and its
  # monthly charge to protect log lines that contain a CPU percentage and a capacity number.
  count = local.is_governor ? 1 : 0

  name              = local.log_group_name
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "governor" {
  # checkov:skip=CKV_AWS_50:X-Ray tracing would need xray:PutTraceSegments added to a role that
  # is deliberately scoped to five actions, to trace a function that makes two API calls.
  # checkov:skip=CKV_AWS_116:A dead letter queue would collect decisions that are already
  # stale. The next invocation is 60 seconds away and recomputes from current CPU, so a
  # replayed failure would apply a capacity decision for load that has since moved on.
  # checkov:skip=CKV_AWS_117:The function calls public AWS endpoints only. Putting it in the
  # VPC would require a NAT gateway or interface endpoints and buy nothing.
  # checkov:skip=CKV_AWS_173:The environment variables below are thresholds and a group name.
  # checkov:skip=CKV_AWS_272:Code signing needs a signing profile and a publishing pipeline;
  # this artifact is built from the repository by the same apply that deploys it.
  count = local.is_governor ? 1 : 0

  function_name = local.function_name
  description   = "Deterministic warm pool controller: reads CPU, writes capacity"
  role          = aws_iam_role.governor[0].arn
  handler       = "governor.handler.lambda_handler"
  runtime       = "python3.13"
  architectures = ["x86_64"]
  filename      = data.archive_file.governor.output_path

  # Without this the function is only redeployed when the filename changes, which it never
  # does -- editing governor/fsm.py would leave the old code running.
  source_code_hash = data.archive_file.governor.output_base64sha256

  # One CloudWatch read and at most two Auto Scaling writes. The decision itself is three
  # comparisons, so nothing here benefits from more memory, and a timeout longer than the
  # polling interval would let two invocations overlap.
  memory_size = 128
  timeout     = 15

  # The controller must never run concurrently with itself. Two invocations reading CPU a
  # moment apart could write different capacities in either order, and the group would end up
  # holding whichever decision happened to land last rather than the most recent one.
  reserved_concurrent_executions = 1

  environment {
    variables = {
      ASG_NAME           = aws_autoscaling_group.app.name
      SCALE_IN_THRESHOLD = tostring(var.scale_in_threshold)
      PRE_WARM_THRESHOLD = tostring(var.pre_warm_threshold)
      METRIC_PERIOD      = tostring(var.metric_period)
      DRY_RUN            = var.dry_run ? "true" : "false"
      LOG_LEVEL          = var.log_level
    }
  }

  # Without this the function can be created before its log group, and Lambda creates an
  # unmanaged group with no retention -- which then blocks the managed one on the next apply.
  depends_on = [aws_cloudwatch_log_group.governor]
}

# The clock. A fixed 60-second tick, not an event-driven trigger: the controller is a sampled
# control loop, and its worst-case reaction time is exactly this interval. Making that a
# configuration value would be false flexibility, because the CloudWatch metric it reads is
# itself published once a minute -- a faster tick would re-read the same datapoint.
resource "aws_cloudwatch_event_rule" "governor_tick" {
  count = local.is_governor ? 1 : 0

  name                = "${var.name_prefix}-governor-tick"
  description         = "Invoke the warm pool controller once a minute"
  schedule_expression = "rate(1 minute)"
  state               = "ENABLED"
}

resource "aws_cloudwatch_event_target" "governor" {
  count = local.is_governor ? 1 : 0

  rule      = aws_cloudwatch_event_rule.governor_tick[0].name
  target_id = "governor"
  arn       = aws_lambda_function.governor[0].arn
}

# EventBridge is not allowed to invoke a function just because it targets it. source_arn pins
# the permission to this one rule rather than to the whole service.
resource "aws_lambda_permission" "governor_tick" {
  count = local.is_governor ? 1 : 0

  statement_id  = "AllowInvocationFromEventBridgeSchedule"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.governor[0].function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.governor_tick[0].arn
}
