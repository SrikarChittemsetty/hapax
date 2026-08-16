# Network. Public subnets only, no NAT gateway anywhere.
#
# A NAT gateway is the single most common way a "free tier" AWS project quietly
# costs $32/month: it bills ~$0.045/hour just for existing, plus data processing,
# and nothing about it is in any free tier. The usual reason to want one is to
# give private instances outbound internet. This design does not have private
# instances — the EC2 box sits in a public subnet with a public IP and reaches
# the internet through the internet gateway, which is free.
#
# RDS is *in* the public subnets but is NOT publicly accessible (see rds.tf).
# It reaches nothing and nothing reaches it except the EC2 security group.

terraform {
  required_version = ">= 1.5"

  required_providers {
    aws    = { source = "hashicorp/aws", version = "~> 5.0" }
    random = { source = "hashicorp/random", version = "~> 3.6" }
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = var.project
      ManagedBy = "terraform"
      # Makes "did teardown actually work?" a one-filter question in the console.
      Teardown = "destroy-all-with-this-tag"
    }
  }
}

data "aws_availability_zones" "available" {
  state = "available"
}

resource "aws_vpc" "main" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true # RDS hands out a DNS name; without this it won't resolve.

  tags = { Name = "${var.project}-vpc" }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${var.project}-igw" }
}

# Two subnets in two AZs. Only one is used by EC2; the second exists because an
# RDS DB subnet group requires subnets in at least two availability zones even
# for a Single-AZ instance. It costs nothing — a subnet is free, and Single-AZ
# means no second database is running.
resource "aws_subnet" "public" {
  count = 2

  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.0.${count.index}.0/24"
  availability_zone       = data.aws_availability_zones.available.names[count.index]
  map_public_ip_on_launch = true

  tags = { Name = "${var.project}-public-${count.index}" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = { Name = "${var.project}-public-rt" }
}

resource "aws_route_table_association" "public" {
  count = length(aws_subnet.public)

  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}
