# Aporia — the second service on the same instance.
#
# Sizing comes from measurement, not a guess. The container idles at 716 MB
# after loading the embedding model and index, and peaks at 769 MB serving
# queries (docker stats, linux/amd64, 2026-08-17). Add the Hapax dispatcher
# (~50 MB) and Amazon Linux itself (~200 MB) and the instance needs ~1.1 GB.
#
# That is why var.instance_type defaults to t3.small (2 GB) when Aporia is
# enabled: a t2.micro has 1 GB, which after the OS leaves roughly 100 MB of
# headroom for a 769 MB process. It would appear to work and then OOM under
# load, which is the worst of both outcomes.
#
# Aporia's own DEPLOY.md previously claimed 1.5 GB. That was double the truth
# and would have pushed this to a t3.medium for no reason.

resource "aws_ecr_repository" "aporia" {
  count = var.enable_aporia ? 1 : 0

  name         = "${var.project}-aporia"
  force_delete = true

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = { Name = "${var.project}-aporia-ecr" }
}

resource "aws_ecr_lifecycle_policy" "aporia_keep_two" {
  count = var.enable_aporia ? 1 : 0

  repository = aws_ecr_repository.aporia[0].name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Keep two images. Aporia's is ~605 MB, so this is the difference between 1.2 GB and unbounded."
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = 2
        }
        action = { type = "expire" }
      },
    ]
  })
}

# The Anthropic key, for the stance layer on claims that are not already cached.
#
# Created empty on purpose. Passing the real key through a Terraform variable
# would write it into terraform.tfstate in plaintext, which is exactly the
# mistake the SSM SecureString exists to avoid. Set the value out of band:
#
#   aws ssm put-parameter --name /hapax/anthropic_api_key --type SecureString \
#     --value "sk-ant-..." --overwrite --region us-east-1
#
# ignore_changes keeps Terraform from reverting that on the next apply.
resource "aws_ssm_parameter" "anthropic_key" {
  count = var.enable_aporia ? 1 : 0

  name        = "/${var.project}/anthropic_api_key"
  description = "Anthropic API key for Aporia's stance layer. Set out of band."
  type        = "SecureString"
  value       = "PLACEHOLDER-set-with-aws-ssm-put-parameter"

  lifecycle {
    ignore_changes = [value]
  }

  tags = { Name = "${var.project}-anthropic-key" }
}

# Aporia serves HTTP. Hapax does not — its dispatcher makes outbound
# connections only — so this is the first ingress rule in the stack that lets
# the internet reach anything.
resource "aws_vpc_security_group_ingress_rule" "aporia_http" {
  count = var.enable_aporia && !var.enable_load_balancer ? 1 : 0

  security_group_id = aws_security_group.ec2.id
  description       = "Aporia HTTP, direct to the instance (no load balancer)"
  cidr_ipv4         = var.aporia_ingress_cidr
  from_port         = 8080
  to_port           = 8080
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "aporia_from_alb" {
  count = var.enable_aporia && var.enable_load_balancer ? 1 : 0

  security_group_id            = aws_security_group.ec2.id
  description                  = "Aporia HTTP from the load balancer only"
  referenced_security_group_id = aws_security_group.alb[0].id
  from_port                    = 8080
  to_port                      = 8080
  ip_protocol                  = "tcp"
}
