<!-- SPDX-FileCopyrightText: 2026 Apoorv Garg <apoorvgarg.21@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Databases

Observal runs two data stores with very different jobs.

| Store | Role | Access pattern | Schema source of truth |
| --- | --- | --- | --- |
| Postgres 16 | Registry, users, config | Relational, transactional | Alembic migrations in `observal-server/alembic/versions/` |
| Telemetry store (DuckDB) | Session events, summaries, audit and security events, webhook deliveries | Columnar; one writer, many readers, HTTP API | `observal-server/telemetry_store/schema/001_baseline.sql` |

## Postgres

### What's in it

* `users`, `roles`, RBAC bindings
* `mcps`, `agents`, `skills`, `hooks`, `prompts`, `sandboxes`: registry metadata
* `reviews`: submission review state
* `feedback`, `ratings`
* `alerts`, `alert_history`
* `api_keys`
* insight reports and caches

### Migrations

Managed by Alembic. The server applies pending migrations automatically on startup. Migration files live in `observal-server/alembic/versions/`.

For Docker Compose deployments, run the init service manually when needed:

```bash
docker compose -f docker/docker-compose.yml run --rm observal-init
```

The init service applies Alembic migrations and the telemetry schema before API startup. `observal server migrate` moves data between deployments; it does not apply schema migrations.

### Reset

To wipe the registry and start over:

```bash
docker compose -f docker/docker-compose.yml down -v
docker compose -f docker/docker-compose.yml up --build -d
```

The `-v` deletes all named volumes. Use only in dev.

---

## Telemetry store

The telemetry store is a **single-writer DuckDB service** (`observal-telemetry`, `python -m telemetry_store`) that owns one database file and exposes it over HTTP on port 8125. The API, worker, Grafana, and CLI never open the file; they call the service with a bearer token. See [Telemetry service](telemetry-service.md) for the service itself and [Data migration](data-migration.md) for moving from ClickHouse.

### What's in it

| Table | Contents | Logical key |
| --- | --- | --- |
| `session_events` | Raw and parsed harness JSONL lines, token fields, tool fields, and session metadata | `(session_key, line_offset)` |
| `session_stats_agg` | One summary row per session (counts, tokens, credits, first/last event time) | `session_key` |
| `session_checkpoints` | Highest contiguous acknowledged source line per session | `session_key` |
| `layer_snapshots` | Harness config snapshots used by version-aware insights (and baseline pins) | `snapshot_key` |
| `audit_log` | Audit events (append-only) | `event_id` |
| `security_events` | Security events for login, auth, and admin activity (append-only) | `event_id` |
| `webhook_deliveries` | Alert webhook delivery attempts and status (append-only) | `delivery_id` |
| `telemetry_import_ledger` | Chunks already imported by `migrate import-telemetry` / the cutover | `(migration_id, chunk_id)` |

`session_key` is a 64-bit hash of `(project_id, user_id, harness, session_id)` computed in `observal_shared.telemetry_keys`. Every identity lookup filters on it, so session reads are integer scans.

### No constraints, no indexes

The schema deliberately has **no primary keys, unique constraints, or indexes**. DuckDB's ART indexes multiply memory and write-ahead-log replay cost at tens of millions of rows and add nothing over zone-map-pruned integer scans. Logical keys are enforced by the writer: a "replace" is `DELETE … WHERE key IN (…)` followed by `INSERT` inside one transaction. `INSERT OR REPLACE` is never used. Tests in `tests/test_telemetry_policy.py` fail the build if either rule is broken.

### Deduplication and summaries

Ingest writes a batch in one transaction: replace the canonical rows for `(session_key, line_offset)`, recompute that session's `session_stats_agg` row, and advance `session_checkpoints`. The summary and checkpoint SQL live once, in `observal-server/telemetry_store/sql.py`, and the same statements drive the post-migration rebuild.

### Retention

Retention runs as worker jobs, not database TTLs:

| What | Setting / default | Job |
| --- | --- | --- |
| Session events | `retention.trace_days`, `retention.max_trace_count` | `run_retention_purge` (every 6 h) |
| `raw_line` transcript bodies | 30 days (`RAW_LINE_RETENTION_DAYS`) | `maintain_telemetry` (every 4 h) sets `raw_line=''`, `raw_line_truncated=2` |
| Audit and security events | 730 days | `maintain_telemetry` |

Deleted blocks are reused after the store's periodic `CHECKPOINT`; the database file does not shrink on its own. To reclaim disk, take a backup (`COPY FROM DATABASE`, see below) and swap the file in.

### Schema changes

There is exactly one schema file, `observal-server/telemetry_store/schema/001_baseline.sql`, applied by the service at start (`python -m telemetry_store.migrate` applies it from an init container). Edit the baseline; do not add versioned files. Never put DDL in application code.

### Capacity planning

At the supported target of 30 million `session_events` (about 300 thousand sessions) the store runs in a 2 GB container with `TELEMETRY_MEMORY_LIMIT=1536MB`. Measured on that dataset (`tests/perf/bench_telemetry.py`): session detail p95 19 ms, session list page 23 ms, executive-dashboard aggregates under 25 ms, a 1 000-row ingest transaction 250 ms. Disk depends on transcript size: budget roughly 1–1.5 KB per event after compression with the 30-day `raw_line` window, plus room for one backup copy.

### External telemetry store

Run the service anywhere with a persistent disk and point the API at it:

```
TELEMETRY_URL=http://telemetry.internal:8125
TELEMETRY_TOKEN=<shared token>
```

Only one service process may own the database file; the service refuses to start with more than one worker.

---

## Backup

See [Backup and restore](backup-and-restore.md). Short version:

* Postgres: `pg_dump` from a running container.
* Telemetry store: `python -m telemetry_store.backup <path>` inside the telemetry container produces an online, snapshot-consistent copy (`COPY FROM DATABASE`); restore by stopping the service and replacing the file.
* Both: back up before every upgrade (`observal server upgrade` does this automatically).

## Next

→ [Telemetry service](telemetry-service.md)
