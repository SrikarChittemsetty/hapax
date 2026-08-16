output "instance_public_ip" {
  description = "SSH here. Changes if the instance is stopped and started (no Elastic IP by design)."
  value       = aws_instance.app.public_ip
}

output "ssh_command" {
  description = "Ready-to-paste SSH command."
  value       = "ssh ec2-user@${aws_instance.app.public_ip}"
}

output "ecr_repository_url" {
  description = "Push the image here."
  value       = aws_ecr_repository.app.repository_url
}

output "rds_endpoint" {
  description = "Not reachable from your laptop by design — only from the app security group."
  value       = aws_db_instance.main.endpoint
}

output "ssm_db_parameter" {
  description = "Where the connection string lives. Read it with: aws ssm get-parameter --name <this> --with-decryption"
  value       = aws_ssm_parameter.db_url.name
}

output "logs_command" {
  description = "Application logs, without CloudWatch."
  value       = "ssh ec2-user@${aws_instance.app.public_ip} 'sudo journalctl -u hapax -f'"
}

output "teardown_checklist" {
  description = "What to confirm in the console after terraform destroy."
  value = join("\n", [
    "After `terraform destroy`, confirm each of these is empty in ${var.region}:",
    "  EC2 > Volumes         — filter Status=Available (unattached volumes bill)",
    "  EC2 > Elastic IPs     — should be none; this stack never allocates one",
    "  RDS > Snapshots       — check BOTH 'Manual' and 'System' tabs",
    "  ECR > Repositories    — '${var.project}' should be gone",
    "  Systems Manager > Parameter Store — '/${var.project}/database_url' should be gone",
    "Then check Billing > Free tier the next day for anything still accruing.",
  ])
}

output "region" {
  description = "Region everything was deployed into; handy for scripting the image push."
  value       = var.region
}
