# The instance's identity.
#
# No access keys anywhere: the instance assumes a role through an instance
# profile, so there is no long-lived credential to leak, rotate, or accidentally
# commit. The role can do exactly two things — pull this one image, read this one
# secret — and both are scoped by ARN rather than by wildcard.

resource "aws_iam_role" "instance" {
  name = "${var.project}-instance"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = { Name = "${var.project}-instance-role" }
}

resource "aws_iam_role_policy" "ecr_pull" {
  name = "${var.project}-ecr-pull"
  role = aws_iam_role.instance.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # GetAuthorizationToken has no resource to scope to — the API is
        # account-wide by design — so it is the one wildcard here, and it only
        # yields a token that the statement below still constrains.
        Effect   = "Allow"
        Action   = "ecr:GetAuthorizationToken"
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "ecr:BatchGetImage",
          "ecr:GetDownloadUrlForLayer",
          "ecr:BatchCheckLayerAvailability",
        ]
        Resource = concat(
          [aws_ecr_repository.app.arn],
          var.enable_aporia ? [aws_ecr_repository.aporia[0].arn] : [],
        )
      },
    ]
  })
}

resource "aws_iam_role_policy" "ssm_read" {
  name = "${var.project}-ssm-read"
  role = aws_iam_role.instance.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = ["ssm:GetParameter", "ssm:GetParameters"]
      # Scoped to exactly the parameters this instance needs, by ARN. The
      # Anthropic key only appears when Aporia is deployed.
      Resource = concat(
        [aws_ssm_parameter.db_url.arn],
        var.enable_aporia ? [aws_ssm_parameter.anthropic_key[0].arn] : [],
      )
    }]
  })
}

resource "aws_iam_instance_profile" "instance" {
  name = "${var.project}-instance"
  role = aws_iam_role.instance.name
}
