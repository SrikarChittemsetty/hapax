# Container registry.
#
# The free allowance is 500 MB of private storage per month for 12 months, which
# a Python image will eat in two or three pushes if nothing expires old ones. The
# lifecycle policy is not housekeeping, it is the thing that keeps this free.

resource "aws_ecr_repository" "app" {
  name = var.project

  # Lets `terraform destroy` delete the repository even when images are in it.
  # Without this, teardown fails and the images keep occupying storage.
  force_delete = true

  image_scanning_configuration {
    # Basic scanning on push is free. (Enhanced scanning, via Inspector, is not.)
    scan_on_push = true
  }

  tags = { Name = "${var.project}-ecr" }
}

resource "aws_ecr_lifecycle_policy" "keep_two" {
  repository = aws_ecr_repository.app.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Keep only the two most recent images; 500 MB/month is the free allowance."
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
