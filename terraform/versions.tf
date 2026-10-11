terraform {
  required_version = ">= 1.9.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
  }
}

# State is deliberately local and there is no backend block. This module stands up a
# short-lived experiment in a single account: a remote backend would introduce an S3 bucket
# and a lock table whose lifecycle outlives the thing being measured, and whoever runs this
# is the only person touching it. A shared or long-lived deployment would add a
# backend "s3" block here before anyone else applied it.
provider "aws" {
  region = var.region

  # An empty string means "fall back to the default credential chain" -- environment
  # variables, SSO, or an instance role. null is how Terraform expresses "argument not set";
  # passing "" straight through would be read as a profile whose name is the empty string.
  profile = var.profile != "" ? var.profile : null

  default_tags {
    tags = var.tags
  }
}
