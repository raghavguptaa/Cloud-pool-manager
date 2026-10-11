# Public entry point and the security boundary around the fleet.
#
# Two security groups rather than one: the load balancer is reachable from the internet, the
# instances are reachable only from the load balancer. That is what stops a warm pool instance
# that has just been started -- and is briefly running user data as root -- from being
# addressable before it is in service.

resource "aws_security_group" "alb" {
  name_prefix = "${var.name_prefix}-alb-"
  description = "Public ingress for the application load balancer"
  vpc_id      = data.aws_vpc.default.id

  tags = {
    Name = "${var.name_prefix}-alb"
  }

  # name_prefix means a replacement creates the new group before destroying the old one, which
  # it cannot do if both carry the same name.
  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_security_group" "instance" {
  name_prefix = "${var.name_prefix}-instance-"
  description = "Application fleet: reachable only from the load balancer"
  vpc_id      = data.aws_vpc.default.id

  tags = {
    Name = "${var.name_prefix}-instance"
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_ingress_rule" "alb_http" {
  # checkov:skip=CKV_AWS_260:A public HTTP endpoint is the system under test -- the load
  # balancer exists to be driven by an external load generator. Narrow allowed_http_cidr to
  # your own address to run the experiment without exposing it to the internet.
  security_group_id = aws_security_group.alb.id
  description       = "HTTP from the public internet"
  cidr_ipv4         = var.allowed_http_cidr
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
}

resource "aws_vpc_security_group_egress_rule" "alb_to_instances" {
  security_group_id            = aws_security_group.alb.id
  description                  = "Health checks and forwarded requests to the fleet"
  referenced_security_group_id = aws_security_group.instance.id
  ip_protocol                  = "tcp"
  from_port                    = 80
  to_port                      = 80
}

resource "aws_vpc_security_group_ingress_rule" "instance_http" {
  # checkov:skip=CKV_AWS_260:This rule carries no cidr_ipv4 at all -- ingress is restricted to
  # the load balancer's security group, which is the narrowest form the API offers. The check
  # reads an absent CIDR as an open one.
  security_group_id            = aws_security_group.instance.id
  description                  = "HTTP from the load balancer only"
  referenced_security_group_id = aws_security_group.alb.id
  ip_protocol                  = "tcp"
  from_port                    = 80
  to_port                      = 80
}

resource "aws_vpc_security_group_egress_rule" "instance_all" {
  # checkov:skip=CKV_AWS_382:Instances need outbound access to the Amazon Linux package
  # repositories and to the EC2 and Systems Manager endpoints while user data runs. Closing
  # this would require interface endpoints for four services in a module whose subject is
  # capacity control, not network isolation.
  security_group_id = aws_security_group.instance.id
  description       = "Package repositories and AWS API endpoints"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

resource "aws_lb" "app" {
  # checkov:skip=CKV2_AWS_20:Redirecting to HTTPS needs an HTTPS listener to redirect to, which
  # needs a certificate and a domain name.
  # checkov:skip=CKV_AWS_91:Access logs need an S3 bucket with its own lifecycle policy. The
  # measurements here come from CloudWatch metrics, not from request logs.
  # checkov:skip=CKV_AWS_150:Deletion protection is exposed as a variable and left off so the
  # experiment can be torn down with one destroy. Turn it on for anything long-lived.
  # checkov:skip=CKV2_AWS_28:A WAF in front of a load generator target would add cost and a
  # request-inspection step to the latency being measured.
  name                       = "${var.name_prefix}-alb"
  load_balancer_type         = "application"
  internal                   = false
  security_groups            = [aws_security_group.alb.id]
  subnets                    = data.aws_subnets.default.ids
  drop_invalid_header_fields = true
  enable_deletion_protection = var.enable_deletion_protection

  tags = {
    Name = "${var.name_prefix}-alb"
  }
}

resource "aws_lb_target_group" "app" {
  # checkov:skip=CKV_AWS_378:Target protocol is HTTP because traffic between the load balancer
  # and the fleet stays inside the VPC and carries no user data.
  name        = "${var.name_prefix}-tg"
  port        = 80
  protocol    = "HTTP"
  vpc_id      = data.aws_vpc.default.id
  target_type = "instance"

  # Short deregistration delay: a scale-in that takes five minutes to drain would dominate the
  # timings this deployment exists to measure.
  deregistration_delay = 30

  # A health check strict enough to notice that an instance is still in its boot delay. This is
  # the mechanism that makes cold-start cost visible in the first place: an instance that is
  # launching, or restarting from the warm pool, fails these checks until the web server binds.
  health_check {
    path                = "/"
    matcher             = "200"
    interval            = 30
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 2
  }

  tags = {
    Name = "${var.name_prefix}-tg"
  }
}

resource "aws_lb_listener" "http" {
  # checkov:skip=CKV_AWS_103:TLS 1.2 enforcement applies to HTTPS listeners; see the skip below
  # for why this one is plain HTTP.
  # checkov:skip=CKV_AWS_2:Plain HTTP by design. Terminating TLS needs a domain and an ACM
  # certificate, and the workload is a synthetic CPU load generator that serves no user data.
  load_balancer_arn = aws_lb.app.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.app.arn
  }
}
