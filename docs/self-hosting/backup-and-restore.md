<!-- SPDX-FileCopyrightText: 2026 Apoorv Garg <apoorvgarg.21@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Backup and restore

What to back up, how often, and how to restore. Observal has three persistent stores with very different loss profiles.

## What matters, in order

| Priority | Volume | Contents | Loss impact |
| --- | --- | --- | --- |
| **Critical** | `apidata` | JWT signing keys | Every session invalidated. No recovery without the keys. |
| **Critical** | `pgdata` | Users, RBAC, registry metadata, agents | All accounts and registry lost. |
| **Important** | `chdata` | Session events, aggregates, audit, and security events | All telemetry lost. Accounts and registry survive. |
| **Low** | `grafanadata` | Custom Grafana dashboards | Custom dashboards lost; provisioned defaults come back automatically. |
| **Low** | `redisdata` | Job queue state | In-flight jobs lost; they re-queue on next worker restart. |

**Always back up `apidata` and `pgdata` together.** Backing up one without the other leaves you in a broken state after restore.

## Backup cadence

| Cadence | What | Retention |
| --- | --- | --- |
| Daily | `pgdata`, `apidata` | 30 days |
| Weekly | `chdata` | 12 weeks |
| Before every upgrade | All three | Keep until the upgrade is confirmed stable |

## Postgres backup

Use `pg_dump` inside the running container:

```bash
docker compose -f docker/docker-compose.yml exec -T observal-db \
  pg_dump -U postgres observal | gzip > observal-pg-$(date +%Y%m%d).sql.gz
```

Restore into a fresh DB:

```bash
docker compose -f docker/docker-compose.yml down -v
docker compose -f docker/docker-compose.yml up -d observal-db

zcat observal-pg-20260421.sql.gz | \
  docker compose -f docker/docker-compose.yml exec -T observal-db \
  psql -U postgres observal

docker compose -f docker/docker-compose.yml up -d
```

## JWT key backup (`apidata` volume)

The API container mounts the `apidata` volume at `/data` and stores keys at `/data/keys`. Tar it out:

```bash
docker compose -f docker/docker-compose.yml exec -T observal-api \
  tar czf - -C /data keys > observal-keys-$(date +%Y%m%d).tar.gz
```

Restore:

```bash
# With the API stopped
docker compose -f docker/docker-compose.yml stop observal-api

docker run --rm \
  -v observal_apidata:/data \
  -v "$(pwd)":/backup \
  alpine sh -c "cd /data && tar xzf /backup/observal-keys-20260421.tar.gz"

docker compose -f docker/docker-compose.yml start observal-api
```

## Telemetry store backup

The telemetry store copies its own database while online. The copy is snapshot-consistent (`COPY FROM DATABASE` in one transaction) and ingest keeps running.

```bash
docker compose -f docker/docker-compose.yml exec -T observal-telemetry \
  /app/.venv/bin/python -m telemetry_store.backup /data/telemetry/backups/observal-$(date +%Y%m%d).duckdb
docker compose -f docker/docker-compose.yml cp \
  observal-telemetry:/data/telemetry/backups/observal-$(date +%Y%m%d).duckdb ./observal-telemetry-$(date +%Y%m%d).duckdb
```

Downtime: none. Throughput is roughly 15–20 MB/s, so budget about 40 minutes for a 40 GB store. The command prints the SHA-256 of the copy; keep it with the file.

Restore:

```bash
docker compose -f docker/docker-compose.yml stop observal-telemetry observal-api observal-worker
docker run --rm -v observal_tdata:/data -v "$(pwd)":/backup alpine sh -c \
  "rm -f /data/observal.duckdb /data/observal.duckdb.wal && cp /backup/observal-telemetry-20260421.duckdb /data/observal.duckdb && chown 1001:1001 /data/observal.duckdb"
docker compose -f docker/docker-compose.yml start observal-telemetry observal-api observal-worker
```

Backing up the whole `tdata` volume with the service stopped also works, but is not needed.

## Restore order

If you're restoring from backup after a catastrophic failure:

1. Stop the whole stack: `docker compose down`.
2. Restore `apidata` (JWT keys) first.
3. Restore `pgdata` (Postgres).
4. Restore `tdata` (telemetry store).
5. Bring up the stack: `docker compose up -d`.
6. Smoke test: `observal auth login`, `observal auth status`.

Skipping step 2 works but every user has to re-login.

## Verifying a backup

Test restores in a staging environment at least quarterly. Untested backups are guesses.

Smoke test after restore:

```bash
observal auth login
observal auth whoami              # you should be your pre-backup user
observal agent list               # registry should be intact
observal ops traces --limit 5     # traces up to the backup timestamp should be visible
```

## Automated backup

A minimal cron setup (on the Docker host):

```cron
# Daily at 03:00 - Postgres + JWT keys
0 3 * * * cd /opt/Observal && \
  docker compose -f docker/docker-compose.yml exec -T observal-db \
  pg_dump -U postgres observal | gzip > /backups/pg-$(date +\%Y\%m\%d).sql.gz

0 3 * * * cd /opt/Observal && \
  docker compose -f docker/docker-compose.yml exec -T observal-api \
  tar czf - -C /data keys > /backups/keys-$(date +\%Y\%m\%d).tar.gz

# Daily at 04:00 - telemetry store (online snapshot, no downtime)
0 4 * * * cd /opt/Observal && \
  docker compose -f docker/docker-compose.yml exec -T observal-telemetry \
    /app/.venv/bin/python -m telemetry_store.backup /data/telemetry/backups/daily.duckdb && \
  docker compose -f docker/docker-compose.yml cp observal-telemetry:/data/telemetry/backups/daily.duckdb \
    /backups/telemetry-$(date +\%Y\%m\%d).duckdb && \
  docker compose -f docker/docker-compose.yml exec -T observal-telemetry rm -f /data/telemetry/backups/daily.duckdb
```

Ship the `/backups` directory offsite (S3, B2, rsync to another host).

## Next

→ [Troubleshooting](troubleshooting.md)
