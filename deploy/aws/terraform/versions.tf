terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
    }
    http = {
      source  = "hashicorp/http"
      version = "~> 3.4"
    }
  }

  # Local state by default. For a shared setup, configure an S3 backend with locking, e.g.:
  # backend "s3" { bucket = "<your-state-bucket>" key = "cerberus/terraform.tfstate" region = "ap-south-1" use_lockfile = true }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = "cerberus"
      ManagedBy = "terraform"
    }
  }
}
