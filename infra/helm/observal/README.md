<!-- SPDX-FileCopyrightText: 2026 Observal Contributors -->
<!-- SPDX-FileCopyrightText: 2026 amogh-dongre <amoghdongre16@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Observal

Observal is an agent-centric registry and observability platform for AI coding agents. This chart deploys the API, web UI, worker, PostgreSQL, the DuckDB telemetry store, Redis, and supporting Kubernetes resources for self-hosted installations.

## Install

After the hosted OCI chart has been published:

```bash
kubectl create namespace observal
helm install observal oci://ghcr.io/observal/charts/observal \
  --version <version> \
  --namespace observal
```

For production deployments, use managed PostgreSQL and Redis services by setting `postgresql.enabled=false` and `redis.enabled=false`, then providing the matching external connection URLs. The telemetry store is a single-writer DuckDB service that always runs as one replica on a persistent volume (`telemetry.storage.size`); point at an externally hosted instance with `telemetry.enabled=false` and `telemetry.externalUrl`.

### Upgrading an install that used ClickHouse

1. `helm upgrade ... --set clickhouse.legacy.enabled=true` keeps the old ClickHouse StatefulSet (and its `chdata` PVC) running next to the new telemetry store. Ingest switches to the telemetry store immediately.
2. Backfill history: `observal server migrate telemetry-cutover --clickhouse-url <CLICKHOUSE_URL from the release Secret> --telemetry-url <TELEMETRY_URL> --telemetry-token <TELEMETRY_TOKEN> --artifact-dir ./cutover` (port-forward both services or run inside the cluster).
3. After verification, `helm upgrade ... --set clickhouse.legacy.enabled=false`. The `chdata` PVC is left in place until you delete it.

See the Kubernetes deployment guide for the full values reference and operational notes:

https://github.com/Observal/Observal/blob/main/docs/self-hosting/kubernetes-helm.md
