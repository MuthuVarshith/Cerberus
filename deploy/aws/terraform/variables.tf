variable "region" {
  description = "AWS region (ap-south-1 = Mumbai)."
  type        = string
  default     = "ap-south-1"
}

variable "instance_type" {
  description = "EC2 size. The Docker sandbox needs ~4 GB RAM; t3.micro (free tier) can host the App but is too small for real verification runs."
  type        = string
  default     = "t3.medium"
}

variable "root_volume_gb" {
  description = "Root disk (OS, Docker images, repository clones)."
  type        = number
  default     = 40
}

variable "repo_url" {
  description = "Git URL the instance clones Cerberus from."
  type        = string
  default     = "https://github.com/MuthuVarshith/Cerberus.git"
}

variable "git_ref" {
  description = "Branch or tag to deploy."
  type        = string
  default     = "upgrade/final"
}

variable "ssm_prefix" {
  description = "SSM Parameter Store path holding the secrets (created by you, not by Terraform)."
  type        = string
  default     = "/cerberus"
}

variable "domain_name" {
  description = "Optional DNS name pointing at the instance. When set, Caddy serves HTTPS with an automatic certificate and ports 80/443 open to the internet; when empty, the App listens on port 8000 restricted to GitHub's webhook IP ranges."
  type        = string
  default     = ""
}

variable "github_app_env" {
  description = "Non-secret App settings written to the service environment."
  type        = map(string)
  default = {
    CERBERUS_MAX_RUNS_PER_HOUR     = "20"
    CERBERUS_RUN_TIMEOUT_SECONDS   = "3600"
    CERBERUS_USER_COOLDOWN_SECONDS = "60"
    CERBERUS_RETRIEVAL             = "bm25"
    LLM_PROVIDER                   = "openrouter"
    LLM_MODEL                      = "poolside/laguna-s-2.1:free"
  }
}
