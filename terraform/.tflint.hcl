# tflint's core rules only check HCL style. The AWS ruleset is what actually reads the
# provider schema, so it is the thing that catches an argument that does not exist or an
# instance type that AWS does not sell -- the failure mode that matters most here, because
# this module was written without a local terraform binary to try it against.
#
# Only the shallow rules run. Deep checking would call the AWS API, and every target in
# this repository is meant to pass with no credentials configured at all.

plugin "terraform" {
  enabled = true
  preset  = "recommended"
}

plugin "aws" {
  enabled = true
  version = "0.48.0"
  source  = "github.com/terraform-linters/tflint-ruleset-aws"
}
