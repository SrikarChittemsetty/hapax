# Postgres. Every setting here that looks conservative is either a free-tier
# boundary or a thing that would survive `terraform destroy` and keep billing.

resource "random_password" "db" {
  length  = 32
  special = true
  # RDS rejects these in a master password.
  override_special = "!#$%&*()-_=+[]{}<>:?"
}

resource "aws_db_subnet_group" "main" {
  name       = "${var.project}-db-subnets"
  subnet_ids = aws_subnet.public[*].id

  tags = { Name = "${var.project}-db-subnets" }
}

resource "aws_db_instance" "main" {
  identifier     = "${var.project}-db"
  engine         = "postgres"
  engine_version = "16"
  instance_class = var.db_instance_class

  allocated_storage = var.db_allocated_storage
  storage_type      = "gp2"
  # max_allocated_storage is deliberately unset. Storage autoscaling would grow
  # past the 20 GB free allowance silently, which is exactly the kind of thing
  # that turns up on a bill rather than in a plan.

  db_name  = var.db_name
  username = var.db_username
  password = random_password.db.result

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.rds.id]

  # The most important line in this file. A publicly accessible RDS instance
  # gets its own public IPv4 address, and public IPv4 costs $0.005/hour for
  # every address — the EC2 free tier's 750 free IPv4 hours apply to EC2 only.
  # So `true` here would add roughly $3.60/month *and* put the database on the
  # internet. It stays false; EC2 reaches it over the VPC's private network.
  publicly_accessible = false

  multi_az = false # Multi-AZ runs a second instance. Never free.

  # Backups off. The free tier includes 20 GB of backup storage, but automated
  # backups are one more thing that can outlive the instance, and this is a
  # portfolio deployment with no data worth keeping.
  backup_retention_period  = 0
  delete_automated_backups = true

  # These two are what make teardown actually leave nothing behind. A final
  # snapshot is a manual snapshot: it survives `terraform destroy` and bills for
  # storage until someone notices it. Turning it off is the right call *here*
  # and would be wrong for anything with real data — say so out loud in an
  # interview rather than presenting it as a default.
  skip_final_snapshot = true
  deletion_protection = false

  # Performance Insights and enhanced monitoring both bill beyond a small free
  # allowance and are easy to leave on. Off.
  performance_insights_enabled = false
  monitoring_interval          = 0

  apply_immediately = true

  tags = { Name = "${var.project}-db" }
}

# The password reaches the container through SSM Parameter Store, not through
# user data, not through an environment variable in the Terraform, and not baked
# into the image. Standard parameters are free (there is no charge for standard
# throughput), and the instance role below can read exactly this one path.
#
# Caveat worth knowing: the generated password IS in terraform.tfstate in
# plaintext. That file is gitignored here, and for anything beyond a portfolio
# deployment it belongs in an encrypted remote backend.
resource "aws_ssm_parameter" "db_url" {
  name        = "/${var.project}/database_url"
  description = "libpq connection string for the Hapax task store"
  type        = "SecureString"
  value = format(
    "postgresql://%s:%s@%s/%s",
    var.db_username,
    urlencode(random_password.db.result),
    aws_db_instance.main.endpoint,
    var.db_name,
  )

  tags = { Name = "${var.project}-db-url" }
}
