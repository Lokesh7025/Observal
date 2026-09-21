<!-- SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Telemetry service

`observal-telemetry` is the single-writer DuckDB service that stores session events, session summaries, audit and security events, and webhook deliveries. It runs from the same image as the API (`python -m telemetry_store`) as exactly one process and exposes an HTTP API on port 8125.

```
api / worker / grafana / cli ──HTTP :8125──▶ observal-telemetry ──▶ /data/telemetry/observal.duckdb
```

## Why one writer

DuckDB is an embedded database: one process owns the file. Instead of letting every API worker open it, the service serialises writes on one thread (each request is one transaction) and serves reads from a bounded thread pool using MVCC snapshots. The API refuses to run if the file is already locked, and `--workers > 1` is rejected at start.

## Endpoints

All endpoints except `/v1/health` require `Authorization: Bearer $TELEMETRY_TOKEN`.

| Endpoint | Purpose |
| --- | --- |
| `GET /v1/health` | Liveness/readiness: schema version, file and WAL size, writer pause state, read queue depth |
| `GET /v1/stats` | Row counts per table, counters, last backfill state |
| `POST /v1/query` | Read-only SQL (`SELECT`/`EXPLAIN` only, named `$params`, per-query `timeout_ms`, result cap) |
| `POST /v1/write/session-batch` | Ingest hot path: replace events + refresh summary + advance checkpoint in one transaction |
| `POST /v1/write/append` / `replace` / `checkpoint` / `delete` / `expire-raw-lines` | Typed writes with server-side table allow-list and key columns |
| `POST /v1/import/chunk` | Idempotent, checksummed Parquet chunk import (ledger keyed by migration + chunk id) |
| `POST /v1/rebuild/derived` | Rebuild `session_stats_agg` and `session_checkpoints` from events (job) |
| `POST /v1/export` + `GET /v1/export/{job}/files/{name}` | Export tables to Parquet chunks with a manifest (job) |
| `POST /v1/admin/backup` | Online snapshot under the server-owned backup root (job) |
| `GET /v1/admin/backup/{job_id}/file` | Download a completed snapshot by opaque job ID |
| `POST /v1/admin/pause-writes` / `resume-writes` / `checkpoint` | Operator controls |
| `GET /v1/jobs/{id}` | Job progress with heartbeat; long operations never sit behind an HTTP timeout |
| `GET /metrics` | Prometheus metrics (query/write latency, timeouts, backpressure, table rows, file size) |

Failure modes are explicit: a slow query is interrupted and returns `504 query_timeout`; too many concurrent reads return `429 telemetry_busy`; a paused writer returns `503 writer_paused`. The API maps these to 503/504/413/429 responses and never substitutes empty data.

## Configuration

| Variable | Default | Notes |
| --- | --- | --- |
| `TELEMETRY_DB_PATH` | `/data/telemetry/observal.duckdb` | Must be on a persistent volume |
| `TELEMETRY_TEMP_DIR` | `<db dir>/tmp` | Spill directory for large sorts/aggregations |
| `TELEMETRY_EXPORT_DIR` | `<db dir>/exports` | Server-owned root where `/v1/export` writes chunks |
| `TELEMETRY_BACKUP_DIR` | `<db dir>/backups` | Server-owned root for online snapshots |
| `TELEMETRY_MAX_IMPORT_CHUNK_BYTES` | `1073741824` | Maximum uploaded Parquet chunk size; larger uploads return 413 |
| `TELEMETRY_MIN_FREE_SPACE_BYTES` | `268435456` | Free-space reserve maintained while staging imports |
| `TELEMETRY_TOKEN` / `TELEMETRY_TOKEN_FILE` | required | Shared bearer token |
| `TELEMETRY_MEMORY_LIMIT` | `1536MB` | DuckDB memory ceiling; keep ~25% below the container limit |
| `TELEMETRY_THREADS` | `4` | DuckDB worker threads |
| `TELEMETRY_READ_THREADS` | `4` | Concurrent read queries |
| `TELEMETRY_READ_QUEUE_MAX` | `64` | Reads waiting before `429` |
| `TELEMETRY_QUERY_TIMEOUT_MS` | `30000` | Default read timeout (interrupted, not abandoned) |
| `TELEMETRY_WRITE_QUEUE_TIMEOUT_MS` | `10000` | How long a write waits while the writer is paused |
| `TELEMETRY_MAX_RESULT_ROWS` | `200000` | `413` above this |
| `TELEMETRY_CHECKPOINT_INTERVAL_S` | `300` | Periodic `CHECKPOINT` |
| `TELEMETRY_BIND` | `0.0.0.0:8125` | |

The API side reads `TELEMETRY_URL`, `TELEMETRY_TOKEN`, `TELEMETRY_TIMEOUT` (30 s), `TELEMETRY_WRITE_TIMEOUT` (60 s), and `TELEMETRY_MAX_CONNECTIONS` (50).

## Sizing

Supported target: 30 million `session_events`, 300 thousand sessions, 2 GB container. Measured on that dataset with `tests/perf/bench_telemetry.py` (4 threads):

| Path | p95 |
| --- | --- |
| Session detail by key | 19 ms |
| Ingest dedup lookup | 13 ms |
| Session list page | 23 ms |
| Executive dashboard aggregates | < 25 ms |
| Full `session_events` group-by scan | 170 ms |
| 1 000-row ingest transaction (events + summary + checkpoint) | 250 ms |
| Online backup | ~17 MB/s (1.7 GB in 100 s; budget ~40 min for a 40 GB store) |

## Backup and restore

```bash
# inside the telemetry container (compose: observal-telemetry)
python -m telemetry_store.backup /data/telemetry/backups/$(date -u +%Y%m%dT%H%M%SZ).duckdb
```

The copy is snapshot-consistent and writes continue during it. The helper asks the service to create the snapshot beneath `TELEMETRY_BACKUP_DIR`, downloads it through an opaque job handle, verifies its size and SHA-256, and atomically writes the requested local path. To restore: stop the service, replace `observal.duckdb` (and delete any `observal.duckdb.wal`), start the service. `observal server upgrade` takes a snapshot automatically and the cloud Terraform modules ship one to object storage nightly.

## Operations

* **Disk does not shrink after deletes.** Freed blocks are reused. Reclaim by backing up and swapping the file.
* **Stuck query.** Every read has a timeout and is interrupted; watch `telemetry_query_timeouts_total`.
* **Backpressure.** `429` from the store means the read pool is saturated; raise `TELEMETRY_READ_THREADS`/`TELEMETRY_READ_QUEUE_MAX` or scale the host.
* **Grafana.** Provisioned dashboards use the Infinity datasource against `/v1/query` with the same token.
* **Logs.** `observal server logs telemetry` (embedded) or `docker compose logs observal-telemetry`.
