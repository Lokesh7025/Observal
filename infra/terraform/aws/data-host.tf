# SPDX-FileCopyrightText: 2026 Apoorv Garg <apoorvgarg.21@gmail.com>
# SPDX-License-Identifier: Apache-2.0

# Data tier: a single EC2 host running the DuckDB telemetry store plus optional
# observability (and, during a cutover, the legacy ClickHouse container).
#
# Why one host: the telemetry store is a single-writer service that owns one
# database file on persistent disk. When bundled observability is enabled,
# Prometheus and Grafana run on the same host. The stack is managed via
# docker-compose bootstrapped from user-data.
#
# Why not an ASG: a 1-instance ASG buys nothing for HA (state lives on EBS, not
# the instance) and complicates DNS. An aws_instance with a static private IP
# attached via ENI gives stable in-VPC addressing. Daily snapshots go to S3.

data "aws_ami" "al2023" {
  most_recent = true
  owners      = ["amazon"]
  filter {
    name   = "name"
    values = ["al2023-ami-2023.*-x86_64"]
  }
  filter {
    name   = "architecture"
    values = ["x86_64"]
  }
}

# tfsec:ignore:aws-ec2-volume-encryption-customer-key AWS-managed aws/ebs key is encrypted at rest; supply a CMK in the production hardening checklist.
resource "aws_ebs_volume" "data" {
  availability_zone = data.aws_subnet.data_host.availability_zone
  size              = local.effective_data_volume_size_gb
  type              = "gp3"
  encrypted         = true

  tags = { Name = "${local.name}-data" }
}

data "aws_subnet" "data_host" {
  id = local.private_subnet_ids[0]
}

# Static private IP via primary ENI — gives the host a stable address that
# survives instance replacement.
resource "aws_network_interface" "data_host" {
  subnet_id       = local.private_subnet_ids[0]
  security_groups = [aws_security_group.data_host.id]

  tags = { Name = "${local.name}-data-host-eni" }
}

locals {
  data_host_user_data = templatefile("${path.module}/user-data.sh.tftpl", {
    region                           = var.region
    ssm_prefix                       = local.ssm_prefix
    image_tag                        = var.image_tag
    image_repo_telemetry             = var.image_repo_telemetry
    log_group                        = aws_cloudwatch_log_group.data_host.name
    backups_bucket                   = aws_s3_bucket.backups.bucket
    grafana_admin_user               = "admin"
    clickhouse_db                    = "observal"
    grafana_root_url                 = local.app_url
    grafana_subpath_prefix           = "/grafana"
    observability_prometheus_enabled = local.observability_prometheus_enabled
    observability_grafana_enabled    = local.observability_grafana_enabled
    enable_legacy_clickhouse         = var.enable_legacy_clickhouse
  })
}

resource "aws_instance" "data_host" {
  ami                  = data.aws_ami.al2023.id
  instance_type        = local.effective_data_instance_type
  iam_instance_profile = aws_iam_instance_profile.data_host.name

  network_interface {
    device_index         = 0
    network_interface_id = aws_network_interface.data_host.id
  }

  user_data                   = local.data_host_user_data
  user_data_replace_on_change = true

  metadata_options {
    http_tokens   = "required"
    http_endpoint = "enabled"
  }

  root_block_device {
    volume_size = 30
    volume_type = "gp3"
    encrypted   = true
  }

  tags = { Name = "${local.name}-data-host" }

  depends_on = [
    aws_nat_gateway.main,
    aws_ssm_parameter.app,
  ]
}

resource "aws_volume_attachment" "data" {
  device_name = "/dev/sdf"
  volume_id   = aws_ebs_volume.data.id
  instance_id = aws_instance.data_host.id
}

# ── Internal DNS so ECS tasks can reach the telemetry store and optional Grafana ──

resource "aws_route53_record" "telemetry_internal" {
  zone_id = aws_route53_zone.internal.zone_id
  name    = local.telemetry_host_internal
  type    = "A"
  ttl     = 60
  records = [aws_network_interface.data_host.private_ip]
}

resource "aws_route53_record" "clickhouse_internal" {
  count   = var.enable_legacy_clickhouse ? 1 : 0
  zone_id = aws_route53_zone.internal.zone_id
  name    = "clickhouse.${var.internal_dns_zone}"
  type    = "A"
  ttl     = 60
  records = [aws_network_interface.data_host.private_ip]
}

resource "aws_route53_record" "grafana_internal" {
  count   = local.bundled_grafana_available ? 1 : 0
  zone_id = aws_route53_zone.internal.zone_id
  name    = "grafana.${var.internal_dns_zone}"
  type    = "A"
  ttl     = 60
  records = [aws_network_interface.data_host.private_ip]
}

# ── State moves ─────────────────────────────────────────────────────────────
# The data host used to be conditional on clickhouse_mode = "self_hosted"
# (count = 1). It is now unconditional; keep existing state addresses so an
# upgrade never destroys the instance or its EBS volume.
moved {
  from = aws_ebs_volume.data[0]
  to   = aws_ebs_volume.data
}
moved {
  from = aws_network_interface.data_host[0]
  to   = aws_network_interface.data_host
}
moved {
  from = aws_instance.data_host[0]
  to   = aws_instance.data_host
}
moved {
  from = aws_volume_attachment.data[0]
  to   = aws_volume_attachment.data
}
moved {
  from = aws_route53_record.clickhouse_internal[0]
  to   = aws_route53_record.telemetry_internal
}
