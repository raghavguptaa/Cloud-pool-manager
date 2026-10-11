# Infrastructure

Terraform for the environment the warm pool controller runs in: a load-balanced Auto Scaling
group, a warm pool, and the Lambda that writes capacity to it once a minute.

One flat root module, no child modules, no remote state. It is meant to be applied, measured,
and destroyed the same afternoon.

```
                 internet
                    |
             [ application LB ]  :80
                    |
        +-----------------------+
        |  Auto Scaling group   |     warm pool (stopped instances)
        |  t3.micro, AL2023     | <-- pre-booted, waiting to be started
        +-----------------------+
                    ^
                    | UpdateAutoScalingGroup / PutWarmPool
                    |
        [ governor Lambda ] <-- EventBridge, rate(1 minute)
                    |
                    | GetMetricData: CPUUtilization, Maximum, 60s
                    v
              CloudWatch
```

## The two modes

`scaling_mode` selects which arm of the comparison gets deployed. They are mutually exclusive:
pointing both a step scaling policy and the controller at one Auto Scaling group means two
things writing capacity, and the measurement becomes a measurement of the argument between
them.

| | `scaling_mode = "governor"` | `scaling_mode = "baseline"` |
|---|---|---|
| Warm pool | yes, `pool_state = "Stopped"` | yes, `pool_state = "Stopped"` |
| **Who sizes the pool** | **the Lambda, every 60 s** | **nobody — fixed at apply time** |
| Instance monitoring | detailed, 1-minute | default, 5-minute |
| What decides capacity | the Lambda, every 60 s | a CloudWatch alarm on a 5-minute average |
| Reaction path | start an already-booted instance | start an already-booted instance |
| Idle cost | pool drains to zero | stopped instances' EBS, billed around the clock |
| Extra cost | ~$2.10/instance/month for metrics | none |

The baseline arm is not a straw man, in two respects.

A 5-minute alarm period is not a tuning mistake, it is the resolution basic monitoring publishes
at, and `evaluation_periods = 1` is the most aggressive setting the mechanism allows. Its latency
is what the mechanism costs.

And **it gets a warm pool too**, because the run this reproduces had one: the recorded timeline in
`../data/baseline_events.csv` extracts a pooled instance 6 s after the alarm fires and has it in
service 1 s later. Denying the reactive arm a warm pool would add ~180 s of cold boot to the thing
under comparison and credit the controller with a difference that was never measured.

That leaves exactly one variable between the arms, which is the point: **who sizes the pool.** The
baseline's is static configuration, so it pays for stopped capacity whether or not any load
justifies it. The governor rewrites `MinSize` and `MaxGroupPreparedCapacity` every 60 s — the
capability AWS has no native equivalent for, since every scaling policy it offers moves
`DesiredCapacity` and nothing else.

## Files

| File | What is in it |
|---|---|
| `versions.tf` | Terraform and provider version constraints, the AWS provider and default tags |
| `variables.tf` | Every knob, with validation. A live re-run should be a `.tfvars` edit |
| `main.tf` | Default-VPC lookups, the AL2023 AMI lookup, and the `is_governor` switch |
| `network.tf` | Two security groups, the load balancer, target group and listener |
| `compute.tf` | Launch template, Auto Scaling group and warm pool; the baseline arm's alarm and step policy; the fleet's instance profile |
| `lambda.tf` | The function's zip, its IAM role, its log group, and the EventBridge schedule |
| `outputs.tf` | Endpoint, group name, and the commands worth running while load is on |
| `user_data.sh.tftpl` | First-boot script: the simulated boot delay, then httpd |
| `example.tfvars` | Annotated starting point. Copy to `terraform.tfvars`, which is gitignored |

## Running it

Needs Terraform >= 1.9 and credentials for an account you are willing to spend a few dollars in.

```sh
cd terraform
cp example.tfvars terraform.tfvars   # auto-loaded; edit region/profile if needed
terraform init
terraform plan
terraform apply
```

To deploy the other arm, change `scaling_mode` in `terraform.tfvars` -- or override it for one
command with `terraform apply -var scaling_mode=baseline`. Apply one arm, measure it, destroy
it, then apply the other.

`apply` prints the load balancer URL. Wait out the boot delay -- about three minutes by default
-- then drive CPU on the fleet and watch the group:

```sh
terraform output -raw app_url
aws logs tail "$(terraform output -raw governor_log_group)" --follow
```

Tear it down with `terraform destroy`. Nothing here is retained
after that except the Lambda's log group contents until the retention window expires.

## What it costs

Rough, ap-south-1, and worth re-checking against current pricing:

| | |
|---|---|
| `t3.micro`, running | ~$0.011/hour, ~$8/month |
| `t3.micro`, stopped in the warm pool | EBS only -- ~$0.75/month for an 8 GiB gp3 root volume |
| Application load balancer | ~$0.0225/hour, ~$16/month, plus a small LCU charge |
| Detailed monitoring | $0.30/metric/month, 7 metrics per instance |
| Lambda | 43,200 invocations/month of a 128 MB function for a few hundred milliseconds -- inside the free tier |

An afternoon of experimenting is well under a dollar. A deployment left running for a month is
dominated by the load balancer, not by the fleet. The load balancer costs the same in both
arms, so it cancels out of any comparison between them -- which is not the same as it being
free. Destroy the stack when you are done.

## Things worth knowing before you change anything

**The controller and Terraform both want to own capacity.** The Lambda calls
`UpdateAutoScalingGroup` and `PutWarmPool` every 60 seconds. Terraform believes `desired_capacity`
and the warm pool sizes are its to manage. Without the `lifecycle { ignore_changes = [...] }`
block on the Auto Scaling group, the next `apply` resets whatever the controller just decided,
and the two fight for as long as anyone keeps applying. The values in the configuration are
initial conditions, not desired state. The same applies in baseline mode, where the step
scaling policy moves capacity.

**Detailed monitoring is not a performance tweak.** At the 5-minute default, a spike and its
recovery are three datapoints. A controller polling every 60 seconds would keep re-reading the
same stale average, and a short burst would never appear at all. It is enabled only in governor
mode, on purpose: turning it on for the baseline arm would improve the thing being measured
against.

**The warm pool's value is in what it does *not* do.** Instances in a `Stopped` warm pool have
already run user data. Starting one skips first boot entirely, which is why
`instance_reuse_policy { reused_on_scale_in = true }` matters -- without it, scale-in terminates
the instance and the pool refills by launching from scratch, paying the cost it exists to avoid.

**The function's role holds five actions.** Two of them -- `UpdateAutoScalingGroup` and
`PutWarmPool` -- are scoped to this one Auto Scaling group's ARN. The other three take no
resource ARN at all on the AWS side, and `lambda.tf` says so at the point where the `"*"`
appears. Logging is a separate, narrower policy rather than the usual managed one, which would
also hand out `logs:CreateLogGroup` account-wide.

**`reserved_concurrent_executions = 1`.** Two invocations reading CPU a moment apart can write
different capacities in either order, and the group keeps whichever landed last rather than the
most recent decision.

## Validation

This module has never been applied from the machine it was written on, and `terraform` is not
installed there. CI is the first thing that executes it: `terraform fmt -check`,
`terraform validate`, `tflint` and `checkov` run on every push. Deliberate `checkov` findings
carry an inline `# checkov:skip=CKV_...` with the reason -- a public HTTP listener, no access
log bucket, no dead letter queue -- rather than being silenced globally.
