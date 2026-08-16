# Every knob that affects the bill is here, with the free-tier consequence of
# changing it written next to it.

variable "region" {
  description = <<-EOT
    Region to deploy into. Keep every resource in one region: ECR-to-EC2 image
    pulls are free within a region and billable across one.
  EOT
  type        = string
  default     = "us-east-1"
}

variable "project" {
  description = "Name prefix for every resource, so teardown is easy to verify in the console."
  type        = string
  default     = "hapax"
}

variable "instance_type" {
  description = <<-EOT
    EC2 instance type.

    READ THIS BEFORE CHANGING OR ACCEPTING THE DEFAULT. On a *legacy* account
    (created before 2025-07-15), the 12-month free tier covers t2.micro in every
    region where t2.micro exists, and t3.micro ONLY in regions where t2.micro is
    not offered. us-east-1 offers t2.micro — so on a legacy account in us-east-1,
    t3.micro is NOT free and bills at roughly $7.50/month.

    On a *new* account (created on or after 2025-07-15) there is no 12-month
    tier at all; usage draws down the $100-$200 credit balance instead, and a
    broader set of instance types is available under the Free plan.

    The default is t2.micro because that is the choice that is free in the most
    situations. Set t3.micro deliberately, knowing which account you have.
  EOT
  type        = string
  default     = "t2.micro"
}

variable "db_instance_class" {
  description = <<-EOT
    RDS instance class. db.t3.micro and db.t4g.micro are both free-tier eligible
    for RDS in all commercial regions (unlike EC2, where t3 is conditional), so
    this default is safe on a legacy account.
  EOT
  type        = string
  default     = "db.t3.micro"
}

variable "db_allocated_storage" {
  description = <<-EOT
    Gigabytes of gp2 storage for RDS. The legacy free tier includes 20 GB; going
    above 20 starts billing at roughly $0.115/GB-month.
  EOT
  type        = number
  default     = 20

  validation {
    condition     = var.db_allocated_storage <= 20
    error_message = "The free tier covers 20 GB of RDS storage. Raise this deliberately, not by accident."
  }
}

variable "root_volume_size" {
  description = <<-EOT
    Root EBS volume in GB. The legacy free tier includes 30 GB of gp2/gp3 across
    the account, and the container image plus Docker needs perhaps 6 GB.
  EOT
  type        = number
  default     = 10

  validation {
    condition     = var.root_volume_size <= 30
    error_message = "The free tier covers 30 GB of EBS. Raise this deliberately, not by accident."
  }
}

variable "ssh_ingress_cidr" {
  description = <<-EOT
    The ONLY CIDR allowed to reach port 22. There is no default on purpose:
    0.0.0.0/0 on SSH is how instances get found and mined within hours.

    Set it to your own address: `curl -s https://checkip.amazonaws.com`/32
  EOT
  type        = string

  validation {
    condition     = var.ssh_ingress_cidr != "0.0.0.0/0"
    error_message = "Refusing to open SSH to the whole internet. Use your own /32."
  }
}

variable "ssh_public_key" {
  description = "Contents of the public key to install on the instance (ssh-ed25519 AAAA...)."
  type        = string
}

variable "db_name" {
  description = "Initial database name."
  type        = string
  default     = "hapax"
}

variable "db_username" {
  description = "Master username for RDS. Not 'admin' or 'postgres' — both are guessed first."
  type        = string
  default     = "hapaxadmin"
}

variable "image_tag" {
  description = "Container image tag in ECR for the instance to run."
  type        = string
  default     = "latest"
}
