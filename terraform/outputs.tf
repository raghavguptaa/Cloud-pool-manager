# The outputs are the handles you need to actually run an experiment: where to send load, what
# to watch while it runs, and which arm you are looking at.

output "app_url" {
  description = "Public endpoint of the fleet. Point a load generator here."
  value       = "http://${aws_lb.app.dns_name}"
}

output "asg_name" {
  description = "Auto Scaling group the controller writes to."
  value       = aws_autoscaling_group.app.name
}

output "scaling_mode" {
  description = "Which arm is deployed. Echoed back so a plan output cannot be misread."
  value       = var.scaling_mode
}

output "governor_function_name" {
  description = "Controller function name, or null in baseline mode."
  value       = one(aws_lambda_function.governor[*].function_name)
}

output "governor_log_group" {
  description = "Controller log group, or null in baseline mode. Every decision is logged here, one line per minute."
  value       = one(aws_cloudwatch_log_group.governor[*].name)
}

output "baseline_alarm_name" {
  description = "Reactive arm's CloudWatch alarm, or null in governor mode."
  value       = one(aws_cloudwatch_metric_alarm.baseline_cpu_high[*].alarm_name)
}

output "watch_capacity_command" {
  description = "Poll the group's capacity and warm pool while load is running."
  value       = "aws autoscaling describe-auto-scaling-groups --auto-scaling-group-names ${aws_autoscaling_group.app.name} --region ${var.region} --query 'AutoScalingGroups[0].[DesiredCapacity,Instances[*].LifecycleState]'"
}
