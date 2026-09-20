<!--
SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com>
SPDX-License-Identifier: Apache-2.0
-->

# Observal — GCP Terraform Module

Deploy a production-ready Observal instance on Google Cloud Platform.

## Architecture

| Component | GCP Service |
|-----------|-------------|
| API / Worker / Init | Cloud Run (v2) + Cloud Run Jobs |
| Web frontend | Cloud Run (v2) |
| PostgreSQL | Cloud SQL |
| Redis | Memorystore |
| ClickHouse, optional Prometheus and Grafana | GCE instance (Docker Compose) |
| Load balancer / TLS | Global HTTPS LB + Managed SSL Certificate |
| DNS | Cloud DNS |
| Secrets | Secret Manager |
| Backups | GCS |
| Logging | Cloud Logging (built-in) |

## Prerequisites

1. A GCP project with billing enabled
2. APIs enabled: `run.googleapis.com`, `sqladmin.googleapis.com`, `redis.googleapis.com`, `compute.googleapis.com`, `secretmanager.googleapis.com`, `dns.googleapis.com`, `vpcaccess.googleapis.com`
3. `gcloud` CLI authenticated
4. Terraform >= 1.5

Enable required APIs:
```bash
gcloud services enable \
  run.googleapis.com \
  sqladmin.googleapis.com \
  redis.googleapis.com \
  compute.googleapis.com \
  secretmanager.googleapis.com \
  dns.googleapis.com \
  vpcaccess.googleapis.com \
  servicenetworking.googleapis.com
```

## Quick Start

```bash
cp terraform.tfvars.example terraform.tfvars
# Edit terraform.tfvars with your project_id and settings

terraform init
terraform plan
terraform apply
```

After apply:
1. Run migrations: `gcloud run jobs execute observal-prod-init --region=us-central1`
2. Access the app at the URL from `terraform output app_url`

## Custom Domain

Set `domain_name` and `dns_managed_zone_name` to enable the Global HTTPS Load Balancer with a managed SSL certificate. The module creates a DNS A record pointing to the LB IP.

## Telemetry store

A GCE instance runs the single-writer DuckDB telemetry store (plus Grafana and Prometheus when `observability_stack` is set) via Docker Compose on a persistent disk. Daily snapshots go to the backups bucket. Access the host via IAP SSH tunnel.

Upgrading an install that used ClickHouse: set `enable_legacy_clickhouse = true`, apply, run `observal server migrate telemetry-cutover` against the `CLICKHOUSE_URL` secret and the `telemetry_endpoint` output, then set it back to `false`. `/data/clickhouse` on the data disk is never deleted by Terraform.

## Accessing the Data Host

```bash
gcloud compute ssh observal-prod-data --zone=us-central1-a --tunnel-through-iap
```

## Outputs

| Output | Description |
|--------|-------------|
| `app_url` | Public URL |
| `cloud_run_urls` | Individual service URLs |
| `data_host_ssh_command` | IAP SSH command for ClickHouse host |
| `init_job_run_command` | Command to re-run migrations |
| `backups_bucket` | GCS backup bucket name |
