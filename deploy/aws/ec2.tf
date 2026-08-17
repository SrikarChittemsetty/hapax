# The application instance.
#
# Note what is NOT here: no Elastic IP. An EIP is a separate billable public
# IPv4, and an EIP that outlives its instance bills at $0.005/hour for doing
# nothing — it is the classic thing left behind after a teardown. The instance
# takes the auto-assigned public IP from its subnet instead, which is covered by
# the EC2 free tier's 750 IPv4 hours/month on a legacy account and disappears
# with the instance.
#
# The cost of that choice: the public IP changes if the instance is stopped and
# started. For a portfolio deployment that is a fair trade for having nothing to
# forget about.

data "aws_ami" "al2023" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-2023.*-kernel-6.1-x86_64"]
  }
}

resource "aws_key_pair" "main" {
  key_name   = "${var.project}-key"
  public_key = var.ssh_public_key

  tags = { Name = "${var.project}-key" }
}

resource "aws_instance" "app" {
  ami           = data.aws_ami.al2023.id
  instance_type = var.instance_type

  subnet_id              = aws_subnet.public[0].id
  vpc_security_group_ids = [aws_security_group.ec2.id]
  iam_instance_profile   = aws_iam_instance_profile.instance.name
  key_name               = aws_key_pair.main.key_name

  associate_public_ip_address = true

  root_block_device {
    volume_size = var.root_volume_size
    volume_type = "gp3"
    encrypted   = true

    # The other classic teardown leftover. Without this, terminating the
    # instance leaves an unattached EBS volume that bills indefinitely.
    delete_on_termination = true
  }

  # Require IMDSv2. Not a cost control — a security one. IMDSv1 lets any SSRF in
  # a running container read the instance role's credentials.
  metadata_options {
    http_tokens                 = "required"
    http_endpoint               = "enabled"
    http_put_response_hop_limit = 2 # 2, so a container (one hop from the host) can still reach it
  }

  user_data = templatefile("${path.module}/user_data.sh.tftpl", {
    region      = var.region
    project     = var.project
    ecr_repo    = aws_ecr_repository.app.repository_url
    image_tag   = var.image_tag
    ssm_db_path = aws_ssm_parameter.db_url.name

    enable_aporia = var.enable_aporia
    aporia_repo   = var.enable_aporia ? aws_ecr_repository.aporia[0].repository_url : ""
    aporia_tag    = var.aporia_image_tag
    ssm_anthropic = var.enable_aporia ? aws_ssm_parameter.anthropic_key[0].name : ""
  })

  # Changing user_data on an existing instance does nothing unless it is
  # replaced; making that explicit avoids "I changed the script and nothing
  # happened".
  user_data_replace_on_change = true

  tags = { Name = "${var.project}-app" }

  lifecycle {
    # Aporia peaks at 769 MB (measured). A micro instance has 1 GB, which after
    # Amazon Linux leaves ~100 MB for a 769 MB process — it boots, serves a few
    # requests, and then the OOM killer takes it. Catching that at plan time is
    # considerably cheaper than diagnosing it at 3am from a health check.
    precondition {
      condition     = !var.enable_aporia || !can(regex("micro$", var.instance_type))
      error_message = "enable_aporia needs >= 2 GB RAM; ${var.instance_type} has 1 GB. Use t3.small or larger."
    }
  }

  depends_on = [aws_db_instance.main]
}
