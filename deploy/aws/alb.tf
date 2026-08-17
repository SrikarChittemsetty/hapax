# Optional load balancer, for a real https:// URL.
#
# Off by default, because it is the single most expensive thing in this stack:
# ~$16/month plus LCU, and nothing in any AWS free tier. With credits it is
# noise; on a free-tier account it is the whole bill.
#
# What it buys, and the reason it exists at all: an ACM certificate and a
# hostname. Without it the demo is http://<changing-ip>:8080, which browsers
# mark insecure and which nobody will paste into an application. With it the
# link is https://aporia.example.com, and the instance stops being reachable
# from the internet at all — the security group only admits the balancer.
#
# The alternative worth knowing about: Caddy on the instance terminates TLS
# with a free Let's Encrypt certificate and costs nothing. It is a genuinely
# reasonable choice here and loses only the managed-certificate rotation and
# the ability to put the instance behind something. Chosen against because with
# credits available the managed path is both simpler to reason about and better
# to be able to talk about.

resource "aws_security_group" "alb" {
  count = var.enable_load_balancer ? 1 : 0

  name        = "${var.project}-alb"
  description = "Public entry point for Aporia"
  vpc_id      = aws_vpc.main.id

  tags = { Name = "${var.project}-alb-sg" }
}

resource "aws_vpc_security_group_ingress_rule" "alb_https" {
  count = var.enable_load_balancer ? 1 : 0

  security_group_id = aws_security_group.alb[0].id
  description       = "HTTPS from anywhere — this is the public demo"
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "alb_http_redirect" {
  count = var.enable_load_balancer ? 1 : 0

  security_group_id = aws_security_group.alb[0].id
  description       = "HTTP, only to redirect to HTTPS"
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 80
  to_port           = 80
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "alb_to_instance" {
  count = var.enable_load_balancer ? 1 : 0

  security_group_id            = aws_security_group.alb[0].id
  description                  = "To the application instance only"
  referenced_security_group_id = aws_security_group.ec2.id
  from_port                    = 8080
  to_port                      = 8080
  ip_protocol                  = "tcp"
}

resource "aws_lb" "main" {
  count = var.enable_load_balancer ? 1 : 0

  name               = "${var.project}-alb"
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb[0].id]
  # Needs two AZs — the same reason the second subnet exists for RDS.
  subnets = aws_subnet.public[*].id

  tags = { Name = "${var.project}-alb" }
}

resource "aws_lb_target_group" "aporia" {
  count = var.enable_load_balancer ? 1 : 0

  name        = "${var.project}-aporia"
  port        = 8080
  protocol    = "HTTP"
  vpc_id      = aws_vpc.main.id
  target_type = "instance"

  health_check {
    path = "/"
    # Aporia loads a model and an index at startup, which takes tens of
    # seconds. A default health check would mark it unhealthy and the balancer
    # would keep cycling it before it ever finished booting.
    healthy_threshold   = 2
    unhealthy_threshold = 5
    timeout             = 10
    interval            = 30
    matcher             = "200"
  }

  tags = { Name = "${var.project}-aporia-tg" }
}

resource "aws_lb_target_group_attachment" "aporia" {
  count = var.enable_load_balancer ? 1 : 0

  target_group_arn = aws_lb_target_group.aporia[0].arn
  target_id        = aws_instance.app.id
  port             = 8080
}

# Certificate. DNS validation rather than email: it is the only kind that can
# be automated, and it renews without anyone clicking a link in two years.
resource "aws_acm_certificate" "main" {
  count = var.enable_load_balancer && var.domain_name != "" ? 1 : 0

  domain_name       = var.domain_name
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }

  tags = { Name = "${var.project}-cert" }
}

# Deliberately not automated: validating the certificate needs a DNS record in
# whatever zone owns the domain, and this stack does not manage that zone. The
# records to create are an output. Terraform waits here until they resolve.
resource "aws_acm_certificate_validation" "main" {
  count = var.enable_load_balancer && var.domain_name != "" ? 1 : 0

  certificate_arn = aws_acm_certificate.main[0].arn
}

resource "aws_lb_listener" "https" {
  count = var.enable_load_balancer && var.domain_name != "" ? 1 : 0

  load_balancer_arn = aws_lb.main[0].arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = aws_acm_certificate_validation.main[0].certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.aporia[0].arn
  }
}

resource "aws_lb_listener" "http" {
  count = var.enable_load_balancer ? 1 : 0

  load_balancer_arn = aws_lb.main[0].arn
  port              = 80
  protocol          = "HTTP"

  # With a certificate, redirect. Without one, serve over plain HTTP so the
  # stack is still usable before a domain exists.
  dynamic "default_action" {
    for_each = var.domain_name != "" ? [1] : []
    content {
      type = "redirect"
      redirect {
        port        = "443"
        protocol    = "HTTPS"
        status_code = "HTTP_301"
      }
    }
  }

  dynamic "default_action" {
    for_each = var.domain_name == "" ? [1] : []
    content {
      type             = "forward"
      target_group_arn = aws_lb_target_group.aporia[0].arn
    }
  }
}
