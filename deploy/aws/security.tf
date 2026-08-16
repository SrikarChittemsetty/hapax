# Security groups. Two rules matter:
#
#   1. SSH is restricted to one CIDR the operator supplies. There is no default,
#      and 0.0.0.0/0 is rejected by a variable validation.
#   2. Postgres is reachable ONLY from the EC2 security group — not from a CIDR,
#      from the *group*. That means "whatever instance is running with this
#      security group", so it keeps working when the instance is replaced and its
#      private IP changes, and it cannot be widened by accident.

resource "aws_security_group" "ec2" {
  name        = "${var.project}-ec2"
  description = "Hapax application instance"
  vpc_id      = aws_vpc.main.id

  tags = { Name = "${var.project}-ec2-sg" }
}

resource "aws_vpc_security_group_ingress_rule" "ssh" {
  security_group_id = aws_security_group.ec2.id
  description       = "SSH from the operator only"
  cidr_ipv4         = var.ssh_ingress_cidr
  from_port         = 22
  to_port           = 22
  ip_protocol       = "tcp"
}

# Deliberately absent: any ingress rule for the application itself. Hapax's MCP
# server speaks JSON-RPC over stdio, not over a socket, so nothing needs to reach
# it from outside. Adding a listener later means adding a rule here — and then
# thinking about TLS, which a load balancer would normally provide and which is
# not free.

resource "aws_vpc_security_group_egress_rule" "ec2_all" {
  security_group_id = aws_security_group.ec2.id
  description       = "Outbound for image pulls, package installs and RDS"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

resource "aws_security_group" "rds" {
  name        = "${var.project}-rds"
  description = "Postgres, reachable only from the application instance"
  vpc_id      = aws_vpc.main.id

  tags = { Name = "${var.project}-rds-sg" }
}

resource "aws_vpc_security_group_ingress_rule" "postgres_from_ec2" {
  security_group_id = aws_security_group.rds.id
  description       = "Postgres from the app security group only"
  # Referencing the security group rather than a CIDR is the point: no address
  # range is ever trusted, only membership of the app's group.
  referenced_security_group_id = aws_security_group.ec2.id
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
}

# No egress rule for RDS. It has no reason to originate connections, and leaving
# the group with no egress rules means it cannot.
