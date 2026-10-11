# One EC2 instance runs the Cerberus GitHub App (FastAPI) as a systemd service, plus Docker for the
# verification sandbox. Secrets live in SSM Parameter Store and are read at boot by the instance role.
# Access is through AWS Systems Manager Session Manager: no SSH key, no port 22.

data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

data "aws_caller_identity" "current" {}

# Latest Amazon Linux 2023 AMI (x86_64), resolved at plan time.
data "aws_ssm_parameter" "al2023" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

# GitHub publishes the IP ranges its webhooks come from.
data "http" "github_meta" {
  url             = "https://api.github.com/meta"
  request_headers = { Accept = "application/json" }
}

locals {
  github_hook_cidrs_v4 = [for c in jsondecode(data.http.github_meta.response_body).hooks : c if !strcontains(c, ":")]
  use_https            = var.domain_name != ""
  ssm_arn_prefix       = "arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter${var.ssm_prefix}"
}

# ------------------------------------------------------------------ network

resource "aws_security_group" "cerberus" {
  name_prefix = "cerberus-app-"
  description = "Cerberus GitHub App: webhook ingress only"
  vpc_id      = data.aws_vpc.default.id

  dynamic "ingress" {
    for_each = local.use_https ? [] : [1]
    content {
      description = "Webhooks on 8000 from GitHub hook ranges only"
      from_port   = 8000
      to_port     = 8000
      protocol    = "tcp"
      cidr_blocks = local.github_hook_cidrs_v4
    }
  }

  dynamic "ingress" {
    for_each = local.use_https ? [80, 443] : []
    content {
      description = "Caddy HTTPS (80 for the ACME challenge and redirect)"
      from_port   = ingress.value
      to_port     = ingress.value
      protocol    = "tcp"
      cidr_blocks = ["0.0.0.0/0"]
    }
  }

  egress {
    description = "Outbound: GitHub API, model APIs, package installs during sandbox setup"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  lifecycle {
    create_before_destroy = true
  }
}

# ------------------------------------------------------------------ IAM

data "aws_iam_policy_document" "assume_ec2" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "cerberus" {
  name_prefix        = "cerberus-app-"
  assume_role_policy = data.aws_iam_policy_document.assume_ec2.json
}

# Session Manager access (replaces SSH).
resource "aws_iam_role_policy_attachment" "ssm_core" {
  role       = aws_iam_role.cerberus.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

# Read only the Cerberus parameters; least privilege.
data "aws_iam_policy_document" "read_secrets" {
  statement {
    actions   = ["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath"]
    resources = [local.ssm_arn_prefix, "${local.ssm_arn_prefix}/*"]
  }
  statement {
    actions   = ["kms:Decrypt"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["ssm.${var.region}.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "read_secrets" {
  name   = "cerberus-read-secrets"
  role   = aws_iam_role.cerberus.id
  policy = data.aws_iam_policy_document.read_secrets.json
}

resource "aws_iam_instance_profile" "cerberus" {
  name_prefix = "cerberus-app-"
  role        = aws_iam_role.cerberus.name
}

# ------------------------------------------------------------------ instance

resource "aws_instance" "cerberus" {
  ami                    = data.aws_ssm_parameter.al2023.value
  instance_type          = var.instance_type
  subnet_id              = data.aws_subnets.default.ids[0]
  vpc_security_group_ids = [aws_security_group.cerberus.id]
  iam_instance_profile   = aws_iam_instance_profile.cerberus.name

  metadata_options {
    http_tokens                 = "required" # IMDSv2 only
    http_put_response_hop_limit = 1          # containers cannot reach instance credentials
  }

  root_block_device {
    volume_size           = var.root_volume_gb
    volume_type           = "gp3"
    encrypted             = true
    delete_on_termination = true
  }

  user_data_replace_on_change = true
  user_data = templatefile("${path.module}/user_data.sh.tftpl", {
    repo_url    = var.repo_url
    git_ref     = var.git_ref
    region      = var.region
    ssm_prefix  = var.ssm_prefix
    domain_name = var.domain_name
    app_env     = var.github_app_env
  })

  tags = {
    Name = "cerberus-app"
  }
}

resource "aws_eip" "cerberus" {
  instance = aws_instance.cerberus.id
  domain   = "vpc"
}
