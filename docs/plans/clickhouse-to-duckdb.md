<!-- SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# ClickHouse → DuckDB migration plan

Status: **PLAN — no code written.** Branch: fresh from updated `main`. PR #1743 is read-only; nothing from it is reused.

## Locked decisions

| # | Decision |
|---|---|
| L1 | DuckDB runs as a **separate single-writer service** (`observal-telemetry`), reached over **HTTP**. The API and worker never open the `.duckdb` file. |
| L2 | Existing ClickHouse installs migrate with **zero telemetry row loss**; verification is by row count + per-chunk SHA-256 + per-table content hash. |
| L3 | All seven deployment modes stay supported: server-package, source Compose, embedded (`observal server`), Helm, AWS (`aws`, `aws-standard`, `aws-ec2`), GCP, Azure. |
| L4 | Rollback keeps **both** volumes (`chdata` and `tdata`) until an operator explicitly deletes ClickHouse. |
| L5 | Supported scale target: **30 M `session_events` rows** (≈300 K sessions), 5 M `audit_log`, 1 M `security_events`, 1 M `webhook_deliveries`, 50 K `layer_snapshots`, DB file ≤ 40 GB, service memory limit 2 GB. |
| L6 | **No `INSERT OR REPLACE`, no `PRIMARY KEY`/`UNIQUE`/`CREATE INDEX` (no ART indexes).** Replace semantics = `DELETE … ; INSERT …` inside one transaction on the single writer. |
| L7 | **One DuckDB schema baseline** (`001_baseline.sql`). No further DuckDB schema versions are created during this work. |
| L8 | **No silent query failures.** Telemetry client raises; routes return HTTP 503 `telemetry_unavailable`; never `return []` on error. |
| L9 | Export, import, backup, and rebuild are **job-style** with progress heartbeats and no interactive HTTP timeout. |
| L10 | Cutover strategy is **switch-then-backfill**: the new API version writes to DuckDB immediately; ClickHouse history is backfilled by an operator-run job. ClickHouse is never written to by the new version. |
| L11 | `session_stats_agg` and `session_checkpoints` are **not imported** from ClickHouse; they are rebuilt from `session_events` after import. |
| L12 | Grafana uses the **Infinity datasource** (JSON-over-HTTP) against the telemetry service's query endpoint; dashboard panel SQL is rewritten to DuckDB dialect. |

---

## 1. Current system map (ClickHouse, as of `main`)

### 1.1 Tables (`observal-server/clickhouse/migrations/001…005`)

| Table | Engine / key | Dedup semantic | Notes |
|---|---|---|---|
| `session_events` | ReplacingMergeTree(`ingested_at`), ORDER BY (project_id, user_id, harness, session_id, line_offset), PARTITION BY sipHash64(identity) % 64 | latest `ingested_at` wins | `raw_line` column TTL 30 days; table TTL from `data.retention_days`; extra rows (`kiro_credits`) use `line_offset=0xFFFFFFFF`, `is_source_record=0`; `rendered=0` for `_ignored`/`_parse_error` |
| `session_checkpoints` | ReplacingMergeTree(`checkpoint_version`) | highest version wins | `checkpoint_version = time.time_ns()` |
| `session_stats_agg` | ReplacingMergeTree(`summary_version`) | rewritten per session by `refresh_session_summary` | one row per (project,user,harness,session) |
| `layer_snapshots` | ReplacingMergeTree(`uploaded_at`), ORDER BY (project_id, user_id, hash) | latest wins | also stores baseline pins with `harness='baseline'`, `hash='baseline:<agent_id>'` |
| `audit_log` | MergeTree, TTL 730d | none | appended by loguru sink in 500-row / 2 s batches |
| `security_events` | MergeTree, TTL 730d | none | single-row inserts |
| `webhook_deliveries` | MergeTree | none | batch inserts |
| `clickhouse_schema_migrations` | MergeTree | — | version ledger |

### 1.2 Writers

| Writer | File | Path |
|---|---|---|
| Session ingest | `services/session_ingest.py` → `services/clickhouse/insert.py` | `insert_session_events`, `insert_session_checkpoint`, `refresh_session_summary` |
| Ingest route | `api/routes/ingest.py` | calls the above; also `insert_session_checkpoint` on integrity repair |
| Audit sink | `services/audit/sink.py`, `services/audit/event_handlers.py`, `services/registry_telemetry.py`, `api/routes/audit.py` | `insert_audit_log` |
| Security events | `services/security_events.py` | raw `INSERT INTO security_events FORMAT JSONEachRow` |
| Webhooks | `services/webhook_delivery.py` | `_insert_webhook_deliveries` |
| Layer snapshots | `api/routes/layer_snapshot.py` | `insert_layer_snapshot` + raw `INSERT … VALUES` for baseline pins |
| Retention | `services/retention.py` | lightweight `DELETE FROM session_events/session_stats_agg` |
| Danger purge | `api/routes/admin/enterprise_settings.py` | `ALTER TABLE … DELETE WHERE project_id` |
| Maintenance | `jobs/maintenance.py::maintain_clickhouse` | `OPTIMIZE TABLE`, `system.parts` health, cron every 4 h |
| Startup | `startup.py` → `services/clickhouse/schema.py::init_clickhouse` | `MATERIALIZE` checks, resource overrides, `MODIFY TTL` |
| Init container | `docker/entrypoint.sh`, `observal_cli/server/orchestrator.py` | `python -m services.clickhouse.migrations` |
| Server-to-server migration import | `observal_shared/migration/ch_import.py`, `jobs/migration.py` | `INSERT … FORMAT Parquet` with `insert_deduplication_token` |

### 1.3 Readers (raw ClickHouse SQL, 27 call sites outside `services/clickhouse/`)

| Reader | File | Tables | CH-specific constructs |
|---|---|---|---|
| Sessions list/detail/summary/stats | `api/routes/sessions.py` | `session_stats_agg`, `session_events` | `FINAL`, `if()`, `countIf`, `toDate/today`, `INTERVAL`, `SETTINGS max_final_threads`, `{p:Type}` params |
| Overview dashboard | `api/routes/dashboard.py` (`_ch_json`) | `session_stats_agg` | `FINAL`, `INTERVAL`; injects `SETTINGS do_not_merge_across_partitions_select_final` |
| Executive dashboard | `api/routes/exec_dashboard.py` (≈45 queries) | `session_stats_agg` | `toStartOfMonth/Week`, `count(DISTINCT)`, `uniqExactIf`, `sumIf`, `dateDiff`, `least/greatest`, string-interpolated `project_id` |
| Audit log | `api/routes/audit_log.py`, `api/routes/admin/policy.py` | `audit_log` | `FORMAT JSONEachRow`, `{lim:UInt32}` |
| Insights | `services/insights/{batch,transcript,session_meta_extractor,version_impact}.py`, `api/routes/insights.py`, `services/insight_version_filters.py` | `session_stats_agg`, `session_events`, `layer_snapshots` | `FINAL`, `anyIf`, `has()`, `ilike`, `round` |
| User profile / recommender | `services/user_profile.py`, `services/registry_recommender.py::build_signal_query`, `services/insights/registry_match.py` | `session_stats_agg`, `session_events` | `lower`, `ilike`, `position` |
| User search | `services/user_search.py::clickhouse_in_condition/clickhouse_user_conditions` | — | builds `{name:String}` placeholders |
| Alerts | `services/alert_evaluator.py` | `session_stats_agg` | bare-text response parsing (`float(r.text)`) |
| Usage ping | `services/usage_ping.py` | `session_stats_agg`, `session_events` | `uniqExact`, `INTERVAL` |
| Retention admin | `api/routes/admin/retention.py` | `session_events` | `count(DISTINCT)`, `INTERVAL` |
| Layer snapshots | `api/routes/layer_snapshot.py` | `layer_snapshots` | `FINAL` |
| Telemetry status | `api/routes/telemetry.py` → `query_recent_events` | `session_stats_agg` | `INTERVAL` |
| Health | `health.py` | — | `SELECT 1` |
| Support bundle | `api/routes/support.py`, `observal_cli/cmd_support.py` | `system.tables`, `version()`, per-table `count()` | system tables |
| Server-to-server migration export | `observal_shared/migration/ch_export.py`, `validation.py`, `telemetry_manifest.py` | all 7 | `sipHash64` sharding, month windows, Parquet via HTTP |
| Grafana | `grafana/dashboards/*.json` (8), `grafana/provisioning/datasources/clickhouse.yaml` | all | `grafana-clickhouse-datasource` |

### 1.4 Config / deployment surfaces

| Surface | Files |
|---|---|
| Server config | `observal-server/config.py` (`CLICKHOUSE_URL`, `_MAX_CONNECTIONS`, `_MAX_KEEPALIVE`, `_TIMEOUT`; secret-file support), `services/clickhouse/client.py` |
| Dynamic settings | `resource.max_query_memory_mb`, `resource.group_by_spill_mb`, `resource.sort_spill_mb`, `resource.join_memory_mb` (`schema.py::RESOURCE_SETTINGS_MAP`; UI in `web/src/pages/admin/settings.tsx`), `data.retention_days`, `retention.*` |
| Source Compose | `docker/docker-compose.yml`, `docker-compose.dev.yml`, `docker-compose.production.yml`, `docker-compose.observability.yml`, `docker/clickhouse/{config.d,users.d}`, `docker/entrypoint.sh`, `Makefile` (`migrate-clickhouse`) |
| Server package | `docker/server-package/{docker-compose.yml,docker-compose.observability.yml,env.template,setup.sh}` (generates CH password + `users.d/generated-password.xml`) |
| Embedded | `observal_cli/server/{constants,deps,config_gen,orchestrator,backup,updater}.py`, `observal_cli/cmd_server.py` (ports 8124/9100, binary download `clickhouse-<os>-<arch>.tar.gz`, `data/ch`, `reset`, `logs`, `status`) |
| Helm | `infra/helm/observal/templates/{clickhouse-service,clickhouse-statefulset,configmap-clickhouse-config,configmap-clickhouse-users,init-job,api-deployment,worker-deployment,secret,_helpers,NOTES}.yaml`, `values.yaml` |
| AWS | `infra/terraform/aws/{clickhouse,ecs,iam,locals,outputs,s3,secrets,security,variables,vpc}.tf`, `user-data.sh.tftpl` (data host runs CH via compose; `clickhouse_mode = self_hosted|cloud`), `aws-standard/{data-host,data-user-data.sh.tftpl,ecs-*}`, `aws-ec2/deploy.sh` |
| GCP | `infra/terraform/gcp/{cloud-run,data-host,locals,outputs,secrets,variables}.tf`, `user-data.sh.tftpl` (`clickhouse_mode`) |
| Azure | `infra/terraform/azure/{clickhouse,container-apps,network,secrets,variables,outputs}.tf`, `cloud-init.yaml.tftpl`, `prod/staging.tfvars` |
| Consistency check | `scripts/check_terraform_consistency.py` |
| Backup | `observal_cli/server/backup.py` (pg_dump + CH schema only — **no CH data backup today**), `docs/self-hosting/backup-and-restore.md` |
| Migration UI | `web/src/pages/admin/dashboard/components/migrate-*.tsx`, `web/src/lib/types/admin.ts::MigrationScope`, `models/migration_job.py`, `api/routes/admin/migrate.py`, `jobs/migration.py`, `observal_cli/cmd_migrate.py` |
| Frontend assumptions | `web/src/pages/user/traces/index.tsx::toDate` parses `"YYYY-MM-DD HH:MM:SS.mmm"`; `admin.ts` health type has `clickhouse: boolean` |
| Tests touching CH | `tests/test_clickhouse_{migrations,resource_tuning,retention}.py`, `test_scope_cleanup_migrations.py`, `test_migration_*.py` (8), `test_migrate*.py`, `test_retention*.py` (5), `test_session_ingest.py`, `test_sessions_api.py`, `test_dashboard_routes.py`, `test_exec_dashboard.py`, `test_audit_logging.py`, `test_enterprise_audit.py`, `test_health.py`, `test_support_*.py` (7), `test_usage_ping.py`, `test_webhook_delivery.py`, `test_layer_snapshot_routes.py`, `test_insights_legacy_version.py`, `test_user_profile_hardening.py`, `test_alert_evaluator.py`, `test_server_{backup,deps,orchestrator,upgrade}.py`, `test_cmd_support.py`, `test_cmd_logs.py`, `test_resilience.py`, `test_secret_files.py`, `observal-server/tests/test_{exec_dashboard,security_events,session_ingest_delivery,user_search}.py`, `observal_cli/tests/test_cmd_migrate.py` |
| Docs | ~45 files under `docs/`, `README.md`, `SETUP.md`, `AGENTS.md`, `CONTRIBUTING.md`, `ROADMAP.md`, `observal-server/README.md`, skills `observal_cli/skills/observal{,-admin}/references/*.md` |

---

## 2. Target architecture

### 2.1 Topology

```
api (N uvicorn workers × M replicas) ─┐
worker (arq)                          ├─ HTTP :8125 ─→ observal-telemetry (1 process, 1 DuckDB file)
grafana (Infinity datasource)         ┘                  /data/telemetry/observal.duckdb
CLI migrate/backup ─→ api ─→ telemetry
```

* `observal-telemetry` is a new Python package **inside the API image**: `observal-server/telemetry_store/` run as `python -m telemetry_store` (uvicorn, `--workers 1` enforced in code: refuse to start if `WEB_CONCURRENCY`/`--workers` > 1).
* One `duckdb.connect(path)` owned by the process. **Writes**: a single dedicated writer thread fed by an `asyncio.Queue`; every write request is one DuckDB transaction. **Reads**: `conn.cursor()` per request executed in a `ThreadPoolExecutor(max_workers=TELEMETRY_READ_THREADS)`; DuckDB MVCC gives snapshot reads concurrent with the writer.
* Postgres stays for relational data; Redis unchanged; no ClickHouse in the default stack.

### 2.2 HTTP contract (`telemetry_store/api.py`)

All endpoints require `Authorization: Bearer <TELEMETRY_TOKEN>` (constant-time compare). Responses are JSON. Errors are `{ "error": { "code", "message", "sql_state"? } }` with 4xx/5xx — never 200-with-empty-body.

| Endpoint | Semantics |
|---|---|
| `GET /v1/health` | `{status, db_path, file_bytes, wal_bytes, memory_limit, writer_paused, queue_depth}` (200/503) |
| `GET /v1/stats` | per-table row counts, db size, last checkpoint, backfill status |
| `POST /v1/query` | `{sql, params: {name: value}, timeout_ms?}` → `{columns, rows:[{…}], row_count, elapsed_ms, truncated}`. Read-only: statement type is checked via `duckdb.extract_statements`; anything but `SELECT`/`WITH`/`DESCRIBE`/`EXPLAIN` → 400. Named `$name` parameters. Default timeout `TELEMETRY_QUERY_TIMEOUT_MS` (30 000); on expiry `conn.interrupt()` → 504 `query_timeout`. Result cap `TELEMETRY_MAX_RESULT_ROWS` (200 000) → 413 `result_too_large`. |
| `POST /v1/write/append` | `{table, rows}` for `audit_log`, `security_events`, `webhook_deliveries`. One transaction. |
| `POST /v1/write/replace` | `{table, rows}`; key columns fixed server-side (§2.4). `DELETE … WHERE key IN (…)` + `INSERT`, one transaction. For `session_events`, `session_checkpoints`, `session_stats_agg`, `layer_snapshots`. |
| `POST /v1/write/session-batch` | `{events:[…], checkpoint?:{…}, refresh_summary: bool}` → replaces events, recomputes `session_stats_agg` for that `session_key` inside **one transaction**, optionally upserts checkpoint. Returns `{events_written, summary}`. This is the ingest hot path — one round-trip, one transaction. |
| `POST /v1/write/delete` | `{table, where: {timestamp_lt?: ts, project_id?: str, session_keys?: [..]}}` — bounded predicate set only; no raw SQL. Used by retention and danger purge. Returns `rows_deleted`. |
| `POST /v1/write/expire-raw-lines` | `{before: ts, day_batch: 1}` — sets `raw_line=''`, `raw_line_truncated=2` for rows older than `before`, one day per transaction. |
| `POST /v1/import/chunk` | multipart: `manifest_entry` (migration_id, chunk_id, table, sha256, row_count) + Parquet body **or** `{path}` on a shared volume. Idempotent via `telemetry_import_ledger` (§4). |
| `POST /v1/rebuild/derived` | `{session_keys?: [...], all?: true}` → rebuild `session_stats_agg` and `session_checkpoints` from `session_events`, in batches of 500 sessions per transaction; job-style (returns `job_id`, poll `GET /v1/jobs/{id}`). |
| `POST /v1/export` | `{tables, dest_dir, since?: ts}` → `COPY (SELECT …) TO 'dest/<table>/<chunk>.parquet'` in ≤ 500 K-row chunks with SHA-256 manifest; job-style. |
| `POST /v1/admin/backup` | `{dest_path}` → `ATTACH dest; COPY FROM DATABASE main TO backup; DETACH` (online, snapshot-consistent, no writer pause); job-style. |
| `POST /v1/admin/checkpoint` | `CHECKPOINT` |
| `POST /v1/admin/pause-writes` / `resume-writes` | writer queue stops dequeuing; queued writers wait up to `TELEMETRY_WRITE_QUEUE_TIMEOUT_MS` (10 000) then 503 `writer_paused` with `Retry-After`. Used by the cutover only. |
| `GET /v1/jobs/{id}` | `{state, pct, message, started_at, finished_at, error}` for export/backup/rebuild jobs. |
| `GET /metrics` | Prometheus: queue depth, query latency histogram, writer txn latency, rows/table, file size, timeouts, 429s. |

Backpressure: read queue depth > `TELEMETRY_READ_QUEUE_MAX` (64) → 429 `telemetry_busy`. Never blocks silently.

### 2.3 API-side client (`observal-server/services/telemetry/`)

```
services/telemetry/
  __init__.py     public surface (below)
  client.py       httpx.AsyncClient pool; raises TelemetryError subclasses
  sql.py          small helpers: in_condition(), interval_ago(), identity_where()
  ids.py          session_key(project_id, user_id, harness, session_id) -> int  (xxh3_64, signed)
  events.py       insert_session_batch(), query_existing_for_dedup(), query_session_checkpoint(),
                  query_source_records_after(), query_session_source_manifest()
  writes.py       append_audit_log(), append_security_events(), append_webhook_deliveries(), replace_layer_snapshot()
  reads.py        query_recent_events() and other shared aggregate readers
```

* `await tq(sql, **params) -> list[dict]` — the one read entry point. Raises `TelemetryUnavailable` (connect/5xx/timeout) or `TelemetryQueryError` (400/504/413). **No caller catches these to return empty results.** A FastAPI exception handler maps them to `503 {"detail":"telemetry_unavailable"}` / `504` / `413`.
* `TelemetryError` for write paths: ingest raises → 503 to CLI (outbox retries). Audit sink and webhook recorder log at `error` and drop (they are best-effort today) — but they log **counts** and the health endpoint exposes `audit_sink_dropped_total`.
* Timestamps in query responses are serialized by the service as `"YYYY-MM-DD HH:MM:SS.mmm"` (UTC) to match ClickHouse `DateTime64(3)` output; numbers are native JSON numbers (ClickHouse returned `UInt64` as strings — Python callers already `int()` them; audit the frontend for `parseInt`-only assumptions in acceptance test A-14).

### 2.4 Final DuckDB schema — `observal-server/telemetry_store/schema/001_baseline.sql`

No constraints, no indexes (L6). Logical keys are enforced by the writer.

```sql
CREATE TABLE IF NOT EXISTS schema_migrations (version VARCHAR, name VARCHAR, applied_at TIMESTAMP);

CREATE TABLE IF NOT EXISTS session_events (
  session_key         BIGINT,        -- xxh3_64(project_id|user_id|harness|session_id); all identity lookups use this
  parent_session_key  BIGINT,        -- NULL when no parent; xxh3_64 of parent identity with same project/user/harness
  session_id          VARCHAR, project_id VARCHAR, user_id VARCHAR, harness VARCHAR,
  agent_id VARCHAR, agent_version VARCHAR, layer_hash VARCHAR,
  line_offset         UBIGINT,       -- 0xFFFFFFFF sentinel preserved for kiro_credits
  source_end_offset   UBIGINT DEFAULT 0,
  line_hash VARCHAR DEFAULT '', source_sha256 VARCHAR DEFAULT '',
  is_source_record    BOOLEAN DEFAULT true,
  rendered            BOOLEAN DEFAULT true,
  event_type          VARCHAR,
  "timestamp"         TIMESTAMP,     -- UTC, ms precision
  uuid VARCHAR, parent_uuid VARCHAR, tool_name VARCHAR, tool_id VARCHAR,
  content_preview VARCHAR DEFAULT '', content_length UINTEGER DEFAULT 0,
  raw_line VARCHAR DEFAULT '', raw_line_truncated UTINYINT DEFAULT 0,   -- 0 none, 1 truncated at ingest, 2 expired
  ingested_at         TIMESTAMP DEFAULT current_timestamp,
  credits DOUBLE DEFAULT 0, parent_session_id VARCHAR,
  input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
  cache_read_tokens INTEGER DEFAULT 0, cache_write_tokens INTEGER DEFAULT 0,
  model VARCHAR DEFAULT ''
);
-- logical key: (session_key, line_offset)

CREATE TABLE IF NOT EXISTS session_checkpoints (
  session_key BIGINT, project_id VARCHAR, user_id VARCHAR, harness VARCHAR, session_id VARCHAR,
  acknowledged_line BIGINT, acknowledged_offset UBIGINT DEFAULT 0,
  checkpoint_version UBIGINT, updated_at TIMESTAMP DEFAULT current_timestamp
);  -- logical key: (session_key)

CREATE TABLE IF NOT EXISTS session_stats_agg (
  session_key BIGINT, project_id VARCHAR, session_id VARCHAR, user_id VARCHAR, harness VARCHAR,
  agent_id VARCHAR DEFAULT '', agent_version VARCHAR DEFAULT '', parent_session_id VARCHAR DEFAULT '',
  layer_hash VARCHAR DEFAULT '',
  first_event_time TIMESTAMP, last_event_time TIMESTAMP,
  event_count BIGINT, prompt_count BIGINT, tool_call_count BIGINT, tool_result_count BIGINT,
  input_tokens BIGINT, output_tokens BIGINT, cache_read_tokens BIGINT, cache_write_tokens BIGINT,
  total_credits DOUBLE, model VARCHAR DEFAULT '',
  summary_version UBIGINT, updated_at TIMESTAMP DEFAULT current_timestamp
);  -- logical key: (session_key)

CREATE TABLE IF NOT EXISTS layer_snapshots (
  snapshot_key BIGINT,  -- xxh3_64(project_id|user_id|hash)
  hash VARCHAR, project_id VARCHAR, user_id VARCHAR, harness VARCHAR,
  content VARCHAR, uploaded_at TIMESTAMP DEFAULT current_timestamp,
  file_count USMALLINT DEFAULT 0, total_size UINTEGER DEFAULT 0, lockfile_hash VARCHAR DEFAULT ''
);  -- logical key: (snapshot_key)

CREATE TABLE IF NOT EXISTS audit_log (
  event_id UUID, "timestamp" TIMESTAMP, actor_id VARCHAR, actor_email VARCHAR, actor_role VARCHAR,
  action VARCHAR, resource_type VARCHAR, resource_id VARCHAR DEFAULT '', resource_name VARCHAR DEFAULT '',
  http_method VARCHAR DEFAULT '', http_path VARCHAR DEFAULT '', status_code USMALLINT DEFAULT 0,
  ip_address VARCHAR DEFAULT '', user_agent VARCHAR DEFAULT '', detail VARCHAR DEFAULT '',
  sensitivity VARCHAR DEFAULT 'standard', request_id VARCHAR DEFAULT '', outcome VARCHAR DEFAULT '',
  duration_ms FLOAT DEFAULT 0, chain_hash VARCHAR DEFAULT '', source VARCHAR DEFAULT 'server'
);

CREATE TABLE IF NOT EXISTS security_events (
  event_id UUID, "timestamp" TIMESTAMP, event_type VARCHAR, severity VARCHAR,
  actor_id VARCHAR DEFAULT '', actor_email VARCHAR DEFAULT '', actor_role VARCHAR DEFAULT '',
  target_id VARCHAR DEFAULT '', target_type VARCHAR DEFAULT '', outcome VARCHAR,
  source_ip VARCHAR DEFAULT '', user_agent VARCHAR DEFAULT '', detail VARCHAR DEFAULT ''
);

CREATE TABLE IF NOT EXISTS webhook_deliveries (
  delivery_id UUID, event_id UUID, alert_rule_id UUID, attempt_number UTINYINT, "timestamp" TIMESTAMP,
  webhook_url VARCHAR, status_code USMALLINT, delivery_status VARCHAR, error VARCHAR,
  duration_ms FLOAT, payload_size UINTEGER
);

CREATE TABLE IF NOT EXISTS telemetry_import_ledger (
  migration_id VARCHAR, chunk_id VARCHAR, "table" VARCHAR, sha256 VARCHAR,
  row_count BIGINT, applied_at TIMESTAMP DEFAULT current_timestamp
);  -- logical key: (migration_id, chunk_id)

CREATE TABLE IF NOT EXISTS telemetry_backfill_state (
  migration_id VARCHAR, phase VARCHAR, pct SMALLINT, message VARCHAR, updated_at TIMESTAMP
);
```

Why `session_key`: with no indexes, every identity lookup is a column scan. A fixed-width BIGINT scan over 30 M rows is ~30–80 ms on one core in DuckDB and late-materializes the wide columns only for matches; string scans are 5–10× slower. Every predicate on session identity is `session_key = $k` (optionally `AND session_id = $sid` as a cheap post-filter on the selection vector). Time-range queries rely on zone maps over `timestamp`/`ingested_at`, which are naturally ordered by append.

`session_key` is computed in **one** place: `observal_shared/telemetry_keys.py` (used by the API client, the telemetry service, and the migration importer) — `xxh3_64` of `"\x1f".join([project_id, user_id, harness, session_id])`, reinterpreted as signed int64.

### 2.5 Connection settings

Server (`observal-server/config.py`; `CLICKHOUSE_*` removed; secret-file variants supported like today):

| Setting | Default | Notes |
|---|---|---|
| `TELEMETRY_URL` | `http://localhost:8125` | compose: `http://observal-telemetry:8125`; embedded: `http://127.0.0.1:8125` |
| `TELEMETRY_TOKEN` / `TELEMETRY_TOKEN_FILE` | required in prod | generated by `setup.sh`, Helm secret, SSM/Secret Manager/Key Vault |
| `TELEMETRY_TIMEOUT` | `30.0` | interactive reads; ingest writes use `60.0` |
| `TELEMETRY_MAX_CONNECTIONS` | `50` | per API process |

Telemetry service (`telemetry_store/settings.py`):

| Setting | Default |
|---|---|
| `TELEMETRY_DB_PATH` | `/data/telemetry/observal.duckdb` |
| `TELEMETRY_TEMP_DIR` | `/data/telemetry/tmp` (spill) |
| `TELEMETRY_MEMORY_LIMIT` | `1536MB` (container limit 2 GB) |
| `TELEMETRY_THREADS` | `4` (DuckDB `threads`) |
| `TELEMETRY_READ_THREADS` | `4` |
| `TELEMETRY_READ_QUEUE_MAX` | `64` |
| `TELEMETRY_QUERY_TIMEOUT_MS` | `30000` |
| `TELEMETRY_WRITE_QUEUE_TIMEOUT_MS` | `10000` |
| `TELEMETRY_MAX_RESULT_ROWS` | `200000` |
| `TELEMETRY_CHECKPOINT_INTERVAL_S` | `300` (also `checkpoint_threshold='256MB'`) |
| `TELEMETRY_BIND` | `0.0.0.0:8125` |

Removed dynamic settings: `resource.max_query_memory_mb`, `resource.group_by_spill_mb`, `resource.sort_spill_mb`, `resource.join_memory_mb` (and their UI card + `apply_resource_settings`). Kept: `data.retention_days`, `retention.*`, `data.cache_ttl_default`.

---

## 3. Behaviour requirements

| Area | Requirement | Implementation |
|---|---|---|
| **Ingestion** | `POST /api/v1/ingest/session` semantics unchanged: dedup by `(identity, line_offset)` with `line_hash` conflict detection, 409 on conflicts at/below checkpoint, repair offsets above checkpoint, extra rows (`kiro_credits`) at sentinel offset, summary refresh, checkpoint advance, final integrity check with `repair_from_line`. | `session_ingest.py` keeps its algorithm; replaces the 4–6 ClickHouse round-trips with: `query_existing_for_dedup` + `query_session_checkpoint` (2 reads, or 1 combined read `/v1/query` with two result sets — keep 2 for simplicity) → `/v1/write/session-batch` (1 write txn: replace events, recompute summary) → `query_source_records_after` loop → checkpoint upsert (folded into the batch when no gap detection is needed: the service computes the contiguous checkpoint inside the same transaction and returns it; API falls back to the loop only if `records == 5000` page boundary — simplification: service endpoint `session-batch` returns `acknowledged_line/offset` computed by a single `SELECT line_offset, source_end_offset … ORDER BY line_offset` gap walk in SQL using window functions). Target: **2 reads + 1 write per ingest call**. |
| **Deduplication** | Replace semantics exactly as ReplacingMergeTree "latest wins" but deterministic: the writer deletes existing keys then inserts. Import chunks are idempotent through the ledger. Append-only tables (audit/security/webhook) dedupe on import by anti-join on `event_id`/`delivery_id` within the chunk transaction. | `writes.py::replace()`; `import/chunk` |
| **Replay** | CLI outbox re-sends after 503/network errors → idempotent because dedup is by content hash. Import chunk replay → ledger hit → 200 `{skipped: true}`. Ambiguous-response retry (client timeout after server commit) is safe for both. | ledger row is inserted in the same transaction as the data |
| **Sessions** | List: `session_stats_agg` filtered (`parent_session_id=''`, `prompt_count>0`, user scope, harness, days, `user_id IN`) ordered by `last_event_time DESC` with `LIMIT/OFFSET`. Detail: identity lookup by `session_id` (string scan on `session_id` — acceptable at 300 K distinct sessions? No: detail lookup receives only `session_id`; resolve via `session_stats_agg` (300 K rows, small) to get `session_key`, then scan events by `session_key`). Subagents: `parent_session_key = $k`. Incremental `after_offset` preserved. `is_active` = `last_event_time > now() - 5 min`. Sentinel clamping (`1971 < ts < 2099`) preserved in SQL and in `_normalize_ts`. | `sessions.py` rewrite |
| **Pagination** | `LIMIT/OFFSET` retained for sessions and audit log (API contract unchanged). Service enforces `TELEMETRY_MAX_RESULT_ROWS`. Session detail is not paginated today (bounded by `MAX_SESSION_TOTAL_LINES`), keep as is; result cap applies. | — |
| **Exports** | (a) Admin server-to-server migration export (Postgres + telemetry) → Parquet chunks ≤ 500 K rows with SHA-256 manifest via `/v1/export` job; (b) insights HTML export unaffected (reads via `tq`); (c) support bundle collects `telemetry_version`, tables, counts, file size. No interactive timeout on (a): CLI polls job status. | `jobs/migration.py`, `observal_shared/migration/telemetry_export.py` |
| **Retention** | Nightly `run_retention_purge`: `DELETE` by day batches for `timestamp < cutoff` (`/v1/write/delete`), orphan `session_stats_agg` rows deleted by anti-join, count-based purge preserved. `raw_line` 30-day expiry → nightly `/v1/write/expire-raw-lines`. Audit/security 730-day TTL → nightly delete. Deleted blocks are reused by DuckDB after `CHECKPOINT`; file does not shrink (documented; `observal server compact` runs backup-copy + swap for reclaim). | `services/retention.py`, `jobs/maintenance.py::maintain_telemetry` (checkpoint + expiry + stats log) |
| **Insights** | All insight readers (`batch`, `transcript`, `session_meta_extractor`, `version_impact`, `registry_match`, `insight_version_filters`) rewritten to DuckDB SQL through `_deps.get_query()` = `tq`. Behavioural parity verified by fixture-based tests comparing outputs on the same seeded dataset. | — |
| **Dashboards** | `dashboard.py`, `exec_dashboard.py` (~45 queries): `toStartOfMonth → date_trunc('month', …)`, `toStartOfWeek → date_trunc('week', …)` (**note** CH `toStartOfWeek` defaults to Sunday; DuckDB `date_trunc('week')` is Monday — pin to Monday and record the change in CHANGELOG), `countIf(c) → count(*) FILTER (WHERE c)`, `sumIf → sum() FILTER`, `uniqExactIf → count(DISTINCT x) FILTER`, `if(a,b,c) → CASE`, `now() - INTERVAL {d:UInt32} DAY → now() - to_days($d)` or `INTERVAL ($d) DAY`, `dateDiff('second',a,b) → date_diff('second',a,b)`, `ilike` native, `has(arr,x) → list_contains`, `anyIf → arg_max/any_value FILTER`, `least/greatest` native, `today() → current_date`. Remove the `project_id = '{project_id}'` string-replace hack: pass `$pid`. | — |
| **Audit log** | Filters and ordering unchanged; `FORMAT JSONEachRow` parsing replaced by `tq`. | `audit_log.py`, `admin/policy.py` |
| **Alerts** | `alert_evaluator` reads numeric scalars from rows instead of `float(r.text)`. | — |
| **Health** | `/health` reports `telemetry: ok|unreachable`; `/api/v1/telemetry/status` uses `query_recent_events`. | `health.py` |
| **Layer snapshots** | replace by `snapshot_key`; baseline pins keep `harness='baseline'`. | — |
| **Security events** | append via client; failure logged at `error` with count (was `debug`). | — |
| **Danger purge** | `/v1/write/delete {project_id}` on `session_events`, `session_stats_agg`, `session_checkpoints`. | — |
| **Usage ping** | rewritten counters. | — |

---

## 4. Migration contract (existing ClickHouse installs)

### 4.1 Actors and artifacts

* Source reader: `observal_shared/migration/ch_export.py` (existing, chunked, sharded by `sipHash64 % 64`, month windows, splits on memory errors, SHA-256 per chunk, manifest v2.0). Renamed `legacy_clickhouse_export.py`; the only ClickHouse code that survives. It uses raw `httpx`, not `services.clickhouse`.
* Importer: new `observal_shared/migration/duckdb_import.py` → posts chunks to `/v1/import/chunk`.
* Orchestration: new CLI command `observal server migrate telemetry-cutover` (+ `--status`, `--resume`, `--verify-only`, `--reverse`) and an admin API job (`operation_type = telemetry_cutover`) with progress in `migration_jobs` so the web UI shows a banner.
* Artifact dir: `<artifact_root>/telemetry-cutover/<migration_id>/{manifest.json, chunks/*.parquet, state.json}`; `manifest.json` holds `source_counts`, `chunk` list with `sha256`, `row_count`, `export_time_cutoff`.

### 4.2 Sequence

```
0. Preflight (fails closed):
   - CH reachable (legacy URL from CLICKHOUSE_URL or --clickhouse-url); telemetry /v1/health ok; API version == N+1.
   - Free disk on artifact volume ≥ 1.2 × CH bytes_on_disk (system.parts) and on telemetry volume ≥ 1.5 × same.
   - telemetry_import_ledger has no other migration_id in progress (or --resume matches).
1. Pause: none required for CH (no writers in N+1). For consistency the exporter freezes export_time_cutoff = now().
   Telemetry writes continue (live ingest to DuckDB). Ingest is NOT interrupted.
2. Export CH → Parquet chunks (session_events, layer_snapshots, audit_log, security_events, webhook_deliveries).
   session_stats_agg and session_checkpoints are exported only for verification counts, not imported (L11).
   Column mapping: UInt8 flags → BOOLEAN; DateTime64 → TIMESTAMP; LowCardinality(String) → VARCHAR; Nullable kept.
   session_key / parent_session_key / snapshot_key are computed by the importer (read_parquet + hash function in Python
   before COPY, or in SQL via a registered scalar UDF — use Python pre-pass writing a sibling Parquet with the key columns
   so the service's INSERT ... SELECT ... FROM read_parquet stays pure SQL).
3. Import each chunk: verify sha256 → POST /v1/import/chunk → service transaction:
      IF ledger has (migration_id, chunk_id) → skip (200, skipped=true)
      session_events: DELETE existing (session_key,line_offset) that appear in the chunk AND have ingested_at <= chunk max ingested_at
                      (never overwrite rows ingested live after cutover); INSERT chunk rows.
      append tables: INSERT ... WHERE NOT EXISTS (same event_id/delivery_id).
      layer_snapshots: replace by snapshot_key where uploaded_at <= chunk's.
      INSERT ledger row.
   Retry policy: 5 attempts, exponential backoff 1–30 s, on network/5xx; on 4xx stop with a clear error. Resume via state.json + ledger.
4. Rebuild derived: POST /v1/rebuild/derived {all:true} — per 500-session batch transaction:
      session_stats_agg  = aggregate from session_events (same formulas as refresh_session_summary)
      session_checkpoints = highest contiguous is_source_record line per session (window-function gap walk),
                            but NEVER lower an existing checkpoint written live after cutover (MAX with existing).
5. Verify:
   - row_count(session_events in DuckDB, ingested_at <= export_time_cutoff) == sum(chunk row_count) == CH `count() FINAL`.
   - per-table content hash: CH `SELECT hex(xxHash64(groupBitXor(...)))`-style order-independent digest is impractical across
     engines; instead compute per-chunk digests over the canonical row projection (sorted by key, columns serialised
     deterministically) on both sides: exporter computes `content_sha256` per chunk from the Parquet rows it wrote;
     importer recomputes from DuckDB `SELECT ... WHERE session_key IN chunk keys AND ingested_at <= cutoff` → must match.
   - session_stats_agg session count within ±0 of CH FINAL count (after excluding sessions with live post-cutover rows).
   - spot checks: 100 random sessions compared event-by-event (line_offset, line_hash, event_type, timestamp).
   Any failure → job state `verify_failed`; nothing is deleted; `--resume` re-runs only failed chunks.
6. Complete: manifest marked `completed_at`; `telemetry_backfill_state` = done; admin banner disappears.
7. Retire (manual, separate command): `observal server retire-clickhouse` stops the CH container / profile; volume kept.
   `observal server retire-clickhouse --delete-volume` requires typed confirmation.
```

### 4.3 Rollback

* **Before cutover completes**: `observal server rollback --to <N>` (existing updater) restarts the previous image; ClickHouse volume untouched; DuckDB volume kept. Telemetry ingested into DuckDB since cutover is retained in `tdata` and can be pushed back with `migrate telemetry-cutover --reverse` (exports DuckDB rows with `ingested_at > cutover` to Parquet via `/v1/export`, imports into CH with existing `ch_import.py` using deterministic `insert_deduplication_token`), then CLI `observal reconcile` fills any remainder from local JSONL.
* **After completion**: same as above; reverse export uses the full DuckDB tables.
* Both volumes are declared in every compose/Helm/Terraform surface for at least two minor releases (`chdata` stays declared but the CH service is under a `legacy-clickhouse` profile / `clickhouse.legacy.enabled` value / `enable_legacy_clickhouse` tf variable, default off after migration).

### 4.4 Fresh installs

Init container runs `python -m telemetry_store.migrate` which applies `001_baseline.sql` if `schema_migrations` lacks it. No ClickHouse anywhere.

---

## 5. Deployment modes

| Mode | Changes |
|---|---|
| **Server package** (`docker/server-package/`) | `docker-compose.yml`: remove `observal-clickhouse` from default services; add `observal-telemetry` (image = api image, `command: ["/app/.venv/bin/python","-m","telemetry_store"]`, `volumes: tdata:/data/telemetry`, `healthcheck: curl -fsS -H "Authorization: Bearer $(cat /run/secrets/telemetry_token)" localhost:8125/v1/health`, `mem_limit ${TELEMETRY_MEMORY_LIMIT:-2G}`, `read_only: true` with `tmpfs /tmp`, `no-new-privileges`); keep `observal-clickhouse` under `profiles: ["legacy-clickhouse"]` with `chdata`; `observal-init`, `api`, `worker` depend on telemetry healthy; `env.template`: `TELEMETRY_URL`, `TELEMETRY_TOKEN_FILE=/run/secrets/telemetry_token`, drop `CLICKHOUSE_*`, `GRAFANA_CLICKHOUSE_PASSWORD_FILE`; `setup.sh`: generate `secrets/telemetry/telemetry_token`, stop generating CH XML unless `--legacy-clickhouse`; `docker-compose.observability.yml`: Grafana plugin `yesoreyeram-infinity-datasource`, datasource `grafana/provisioning/datasources/telemetry.yaml` with bearer header. |
| **Source Compose** (`docker/`) | Same edits to `docker-compose.yml`, `.dev.yml`, `.production.yml`, `.observability.yml`; `docker/clickhouse/` kept only for the legacy profile; `entrypoint.sh` runs `python -m telemetry_store.migrate` instead of `services.clickhouse.migrations`; `Makefile`: `migrate-clickhouse` → `migrate-telemetry`; `Dockerfile.api` adds `duckdb` dependency (already needs `pyarrow`); add `curl` to runtime image for the healthcheck (or use a Python one-liner). |
| **Embedded** (`observal server`) | `constants.py`: `TELEMETRY_PORT = 8125`, data path `DATA_DIR / "telemetry"`, drop CH ports/version/binary from required deps; `deps.py`: `is_installed()` no longer requires clickhouse; CH binary download becomes `ensure_legacy_clickhouse()` used only by `migrate telemetry-cutover` when `DATA_DIR/ch` exists; `config_gen.py`: `generate_telemetry_env()` instead of CH XML; `orchestrator.py`: `start_telemetry()` spawns `python -m telemetry_store` from the server venv with env, waits on `/v1/health`, `stop_telemetry()`, status/logs entries; `_run_migrations` runs `telemetry_store.migrate`; `cmd_server.py`: `reset` deletes `telemetry`, `logs telemetry`, `status`; `backup.py`: pg_dump + `/v1/admin/backup` file into the backup dir; `restore_backup` stops telemetry, swaps file, starts. |
| **Helm** (`infra/helm/observal/`) | Add `telemetry-statefulset.yaml` (1 replica, `podManagementPolicy: OrderedReady`, PVC `telemetry` RWO, resources 2 Gi limit, probes on `/v1/health`), `telemetry-service.yaml` (ClusterIP 8125), token in `secret.yaml`; `configmap-env.yaml`: `TELEMETRY_URL`; `init-job.yaml`: init container waits for telemetry; remove CH templates from default rendering, keep them behind `clickhouse.legacy.enabled` (default `false`) for the migration window; `values.yaml`: `telemetry: {image, resources, persistence.size: 50Gi, memoryLimit, queryTimeoutMs}`; `NOTES.txt`, `_helpers.tpl`, `README.md`. `helm lint` + `helm template` golden files in `tests/test_helm_render.py`. |
| **AWS `aws`** (ECS + data-host EC2) | `clickhouse.tf` → `data-host.tf`: same EC2 + EBS; `user-data.sh.tftpl` runs compose with `observal-telemetry` (api image tag from SSM) mounting `/data/telemetry`, plus optional Grafana; CH service present only when `enable_legacy_clickhouse = true`; SG: port 8125 from ECS tasks SG (remove 8123 unless legacy); Route53 `telemetry.<zone>`; SSM params `TELEMETRY_URL`, `TELEMETRY_TOKEN` (`secrets.tf`); `ecs.tf` env `TELEMETRY_URL` + secret `TELEMETRY_TOKEN`; drop `clickhouse_mode` variable — `cloud` mode is **not** supported (users on ClickHouse Cloud run the cutover export against the cloud URL, then decommission); `outputs.tf`, `README.md`, `terraform.tfvars.example`. |
| **AWS `aws-standard`** | `data-host.tf`, `data-user-data.sh.tftpl`, `ecs-tasks.tf`, `ecs-services.tf`, `secrets.tf`, `security.tf`, `outputs.tf`, `variables.tf` — same substitutions. |
| **AWS `aws-ec2`** | Single host uses server-package compose; `deploy.sh` generates `TELEMETRY_TOKEN` instead of `CLICKHOUSE_PW`, copies no CH configs. |
| **GCP** | `data-host.tf` + `user-data.sh.tftpl` run telemetry container on the GCE data host with persistent disk at `/data/telemetry`; `cloud-run.tf` env `TELEMETRY_URL`, secret `TELEMETRY_TOKEN` (`secrets.tf`); firewall 8125 from serverless VPC connector range; drop `clickhouse_mode`/`clickhouse_cloud_url`; `variables.tf` `data_disk_size_gb` retained. |
| **Azure** | `clickhouse.tf` → `data-host.tf` (VM + managed disk kept, created when telemetry or redis self-hosted); `cloud-init.yaml.tftpl` runs telemetry container; `container-apps.tf` env/secret; `network.tf` NSG 8125; `secrets.tf` Key Vault `telemetry-token`; `prod.tfvars`, `staging.tfvars`, `README.md`. |
| **Consistency** | `scripts/check_terraform_consistency.py` updated to assert `TELEMETRY_URL`/`TELEMETRY_TOKEN` present in every provider and no `CLICKHOUSE_URL` outside legacy blocks. |
| **Grafana** (all modes) | `grafana/provisioning/datasources/telemetry.yaml` (Infinity; URL `http://observal-telemetry:8125`, bearer header from `TELEMETRY_TOKEN`); 8 dashboards rewritten: each panel = Infinity query, method POST, URL `/v1/query`, body `{sql: "<DuckDB SQL>", params: {...}}`, root selector `rows`. Fallback if Infinity proves inadequate for time-series panels: `/metrics` Prometheus gauges + Prometheus datasource (decide in step 10 with screenshots). |

Ports and volumes doc (`docs/self-hosting/ports-and-volumes.md`): `8125/tcp` internal only; volume `tdata` (`/data/telemetry`); `chdata` legacy.

---

## 6. Performance limits (scale target L5)

| Dimension | Limit / expectation | Enforcement |
|---|---|---|
| Rows | 30 M `session_events`; 300 K sessions; 5 M audit; 1 M security; 1 M webhook; 50 K snapshots | benchmark dataset generator `tests/perf/gen_telemetry.py` (seeded, realistic harness mix, 100-line median sessions, 2 KB median `raw_line`) |
| File size | ≤ 40 GB with 30-day `raw_line` window | nightly expiry job; `/v1/stats` exposes `file_bytes`; alert threshold documented |
| Memory | container 2 GB; DuckDB `memory_limit=1536MB`; spill to `TELEMETRY_TEMP_DIR`; read pool 4 threads | `SET memory_limit`, `SET temp_directory`, `SET threads` at connect; OOM test in perf suite |
| Latency targets (30 M rows, warm) | session detail (500 events) p95 < 500 ms; session list p95 < 300 ms; ingest request (1000 lines) p95 < 750 ms; exec dashboard endpoint p95 < 2 s; audit log page p95 < 300 ms; alert/usage-ping scalars < 1 s | `tests/perf/bench_queries.py` — nightly CI job, fails on > 1.5× regression |
| Throughput | 200 ingest requests/min sustained (60/min/user rate limit × concurrent users) → ≈ 3 write txns/s; single writer keeps ≥ 20 txns/s of 1 000-row batches | perf suite `bench_ingest.py` |
| Timeouts | interactive query 30 s → interrupt → 504; write queue wait 10 s → 503; export/import/backup/rebuild: none (job + heartbeat every 5 s; job aborts if no progress for 30 min) | service settings; `TelemetryClient` uses `timeout=None` for job polling bodies |
| Concurrency | API: up to `API_WORKERS`×replicas clients; service: 4 read threads, queue 64 → 429 above; one writer | load test with 32 concurrent readers + writer |
| Backup | `COPY FROM DATABASE` 40 GB → ≤ 15 min on gp3/pd-balanced; online, no writer pause; restore = file swap ≤ 2 min | perf suite measures; docs state expectation |
| Cutover | export 30 M rows from CH ≤ 30 min; import ≤ 30 min; rebuild ≤ 10 min; verify ≤ 10 min; total ≤ 90 min with live ingest continuing | perf suite `bench_cutover.py` against a seeded ClickHouse container (nightly only) |
| Retention delete | day-batched deletes ≤ 5 s per day batch; checkpoint after purge | maintenance job timing logged |

---

## 7. Acceptance matrix (all must pass before merge)

| ID | Test | Location | Gate |
|---|---|---|---|
| A-01 | Telemetry service unit: schema baseline idempotent; `schema_migrations` recorded once; second start no-op | `observal-server/tests/telemetry_store/test_schema.py` | PR |
| A-02 | Replace semantics: two writes same key → one row, latest content; sentinel `0xFFFFFFFF` row preserved; batch with 1 000 rows + summary in one txn (failure mid-batch leaves nothing) | `test_writer.py` | PR |
| A-03 | Read-only enforcement: `/v1/query` rejects INSERT/DELETE/CREATE/ATTACH/COPY/PRAGMA/SET; param injection test | `test_query_endpoint.py` | PR |
| A-04 | Timeout: long query (`range(1e9)` cross join) with `timeout_ms=200` → 504 within 1 s, connection reusable after | `test_timeouts.py` | PR |
| A-05 | Backpressure: 100 concurrent slow reads → some 429, none hang; queue metric matches | `test_backpressure.py` | PR |
| A-06 | Auth: missing/wrong token → 401 for every route incl. `/metrics`? (`/metrics` allowed unauthenticated only when `TELEMETRY_METRICS_PUBLIC=true`) | `test_auth.py` | PR |
| A-07 | Single-writer guard: starting with `--workers 2` / `WEB_CONCURRENCY=2` exits non-zero; second process on same file exits with clear lock error | `test_single_writer.py` | PR |
| A-08 | Import ledger idempotency: same chunk twice → second skipped, counts unchanged; chunk with bad sha256 → 422, nothing written; crash between data insert and ledger insert simulated → rollback | `test_import_chunk.py` | PR |
| A-09 | Live-overlap rule: rows ingested after cutoff are never overwritten by an imported chunk of the same key | `test_import_overlap.py` | PR |
| A-10 | Rebuild derived: stats formulas equal `refresh_session_summary` output for 50 fixture sessions; checkpoint never regresses | `test_rebuild.py` | PR |
| A-11 | Backup: `COPY FROM DATABASE` while writes continue; restored file row counts == snapshot; WAL cleanly checkpointed | `test_backup.py` | PR |
| A-12 | Retention: day-batched delete, orphan stats cleanup, raw_line expiry sets `raw_line_truncated=2`, audit 730 d; count-based purge | `tests/test_retention*.py` (rewritten) | PR |
| A-13 | API route tests run against a **real in-process DuckDB** (`telemetry_store` app mounted via `httpx.ASGITransport`, `:memory:` or tmp file) — no mocked SQL: sessions list/detail/summary/stats, dashboard overview, all exec dashboard endpoints, audit log + policy, insights count/facets/transcript/version impact, user profile, alert evaluator, usage ping, retention admin, layer snapshot CRUD + baseline pin, telemetry status, health, support collectors, danger purge, security events, webhook deliveries | `tests/conftest.py` fixture `telemetry_stack`; existing test files rewritten | PR |
| A-14 | Response shape parity: for every route in A-13, snapshot JSON compared to a recorded ClickHouse-era snapshot from the same seeded dataset (timestamps `"YYYY-MM-DD HH:MM:SS.mmm"`, numeric types); documented deltas only (`toStartOfWeek` Monday) | `tests/parity/` | PR |
| A-15 | No-silent-failure lint: AST test asserts no `except` around `tq(`/telemetry client calls that returns `[]`, `{}`, `0`, or `None`; every route surfaces 503 when the telemetry fixture is stopped | `tests/test_no_silent_telemetry_failures.py` | PR |
| A-16 | No-ClickHouse lint: `rg -i clickhouse` across `observal-server/`, `web/src/`, `docker/`, `infra/` returns only allow-listed legacy files (`legacy_clickhouse_export.py`, `ch_import.py`, legacy compose profile, migration docs, CHANGELOG) | `tests/test_no_clickhouse_references.py` | PR |
| A-17 | No forbidden DDL: schema and code contain no `INSERT OR REPLACE`, `PRIMARY KEY`, `UNIQUE`, `CREATE INDEX`, `CREATE UNIQUE INDEX`; only one file in `telemetry_store/schema/` | `tests/test_duckdb_constraints_policy.py` | PR |
| A-18 | Ingest end-to-end with CLI session delivery for all 10 harness fixtures (`test_*_session_delivery.py`) against real DuckDB; 503 during writer pause → outbox retry succeeds after resume; 409 conflict path; final integrity + repair path | rewritten delivery tests | PR |
| A-19 | Cutover integration: seeded ClickHouse container (testcontainers, marked `integration`) with 200 K events across 64 shards, 3 months, kiro credits rows, layer snapshots incl. baseline pins, audit/security/webhook rows → run `migrate telemetry-cutover` while a live ingest loop writes to DuckDB → verify counts/digests, resume after killed import, reverse migration back to CH, rollback image switch | `tests/integration/test_telemetry_cutover.py` | nightly + required before merge (manual run recorded in PR) |
| A-20 | Perf suite at 30 M rows: all §6 latency targets; memory stays under 2 GB; backup ≤ 15 min | `tests/perf/` | nightly; results attached to PR |
| A-21 | Fresh-install smoke for each deployment surface: `docker compose up` (source), server-package `setup.sh` + `up`, `observal server install && start && status` (embedded, Linux + macOS), `helm lint && helm template` goldens, `terraform validate` for `aws`, `aws-standard`, `aws-ec2`, `gcp`, `azure` + `check_terraform_consistency.py` | CI jobs | PR |
| A-22 | Upgrade smoke: server-package N (ClickHouse) with data → upgrade to N+1 → ingest works immediately → cutover job → CH profile stopped → volumes both present | `tests/e2e/upgrade-cutover.sh` | nightly + manual before merge |
| A-23 | Playwright: traces list + detail + live update, admin dashboard, exec dashboard, audit log page, migration banner/progress, health page | `tests/e2e/telemetry-*.spec.ts` | PR (against running stack) |
| A-24 | Grafana: provisioning loads Infinity datasource; each of 8 dashboards renders with data (screenshot per dashboard attached to PR per AI_POLICY) | manual + `tests/e2e/grafana.spec.ts` | PR |
| A-25 | Support bundle contains `telemetry_version`, table counts, file size, and redacts the token | `tests/test_support_*.py` | PR |
| A-26 | Docs build: no broken links; `docs/self-hosting/databases.md`, `data-migration.md`, `backup-and-restore.md`, `resource-tuning.md`, `ports-and-volumes.md`, `troubleshooting.md`, `upgrades.md` updated; skills `server-operations.md`, `commands.md` reflect new commands | docs CI | PR |
| A-27 | `make lint`, `make test`, `observal-server/tests`, `observal_cli/tests`, `make test-fuzz` green | CI | PR |

---

## 8. Implementation order and file map (for Fable)

Work on one branch `feat/duckdb-telemetry` cut from updated `main`. Commit in the order below; each commit must keep `make test` green. Merge as one PR (or a stacked series in this order); no ClickHouse code path is deleted before step 8, and after step 8 none remains except the allow-list.

### Step 1 — shared keys and constants (`packages/observal-shared/`)
* `observal_shared/telemetry_keys.py` (new): `session_key()`, `parent_session_key()`, `snapshot_key()`; add `xxhash` dependency to `packages/observal-shared/pyproject.toml`.
* `observal_shared/migration/constants.py`: add `TELEMETRY_TABLES` registry (name, logical key columns, time column, append/replace) used by service and importer; keep `CLICKHOUSE_TABLES` for the legacy exporter.
* Tests: `tests/test_telemetry_keys.py` (vectors, stability, signed range).

### Step 2 — telemetry service (`observal-server/telemetry_store/`)
```
telemetry_store/
  __main__.py        uvicorn entry, single-worker guard, file lock
  settings.py        env parsing (§2.5)
  db.py              connection, PRAGMA/SET, checkpoint loop, interrupt helper
  writer.py          queue + writer thread; transaction wrapper; replace/append/delete/expire/session-batch/import/rebuild
  reader.py          thread pool, statement-type check, param binding, row serialisation (timestamps → "YYYY-MM-DD HH:MM:SS.mmm")
  jobs.py            in-process job registry (export, backup, rebuild) with heartbeat + persistence in telemetry_backfill_state
  api.py             FastAPI routes (§2.2), auth dependency, error envelope
  metrics.py         prometheus_client registry
  migrate.py         apply schema/001_baseline.sql; `python -m telemetry_store.migrate`
  schema/001_baseline.sql
  sql/summary.sql    canonical session_stats_agg aggregation (used by session-batch and rebuild — one source of truth)
  sql/checkpoint.sql canonical contiguous-checkpoint window query
```
* Dependencies: `duckdb>=1.2`, `prometheus_client`, `filelock` in `observal-server/pyproject.toml` (+ `uv.lock`).
* `docker/Dockerfile.api`: no structural change (same image); ensure `duckdb` wheel present.
* Tests A-01…A-11 in `observal-server/tests/telemetry_store/` and wire that directory into `make test`.

### Step 3 — API client (`observal-server/services/telemetry/`)
* Files per §2.3; `config.py`: add `TELEMETRY_*`, keep `CLICKHOUSE_*` until step 8.
* `app_factory.py`: register `TelemetryError` exception handlers.
* `tests/conftest.py`: `telemetry_stack` fixture (in-process service via `ASGITransport`, tmp DuckDB file) and `telemetry_client` patched onto `services.telemetry.client`.
* Tests: client errors, param binding, timestamp parsing, 503 mapping.

### Step 4 — ingest path
* `services/session_ingest.py`: swap imports to `services.telemetry`; use `insert_session_batch` (events + summary + checkpoint in one txn); keep algorithm and public signatures.
* `api/routes/ingest.py`: `insert_session_checkpoint` → `services.telemetry.events.upsert_checkpoint`; unchanged contract.
* `services/session_parsers/kiro.py`: unchanged (sentinel offset retained).
* Tests: `tests/test_session_ingest.py`, all `test_*_session_delivery.py`, `test_session_outbox.py`, `test_session_reconcile.py`, `observal-server/tests/test_session_ingest_delivery.py` → real DuckDB (A-18).

### Step 5 — readers, one module at a time (each with its rewritten tests)
1. `api/routes/sessions.py` (+ `services/user_search.py`: `clickhouse_in_condition` → `telemetry_in_condition` producing `$user_0…`; callers `admin/policy.py`, `sessions.py`)
2. `api/routes/dashboard.py` (`_ch_json` → `tq`; remove SETTINGS/project_id hacks) and `api/routes/exec_dashboard.py` (+ `tests/test_exec_dashboard.py`, `test_dashboard_*.py`, `observal-server/tests/test_exec_dashboard.py`, `scripts/seed_exec_dashboard.py`)
3. `api/routes/audit_log.py`, `api/routes/admin/policy.py`, `api/routes/audit.py`, `services/audit/{sink,event_handlers}.py`, `services/registry_telemetry.py`
4. `services/insights/{_deps,batch,transcript,session_meta_extractor,version_impact,registry_match}.py`, `services/insight_version_filters.py`, `api/routes/insights.py`, `services/insights/__init__.py` (configure `query=tq`)
5. `services/user_profile.py`, `services/registry_recommender.py::build_signal_query`
6. `services/alert_evaluator.py`, `services/usage_ping.py`, `api/routes/telemetry.py`
7. `api/routes/layer_snapshot.py`, `services/security_events.py`, `services/webhook_delivery.py`
8. `api/routes/admin/retention.py`, `services/retention.py`, `api/routes/admin/enterprise_settings.py` (danger purge; delete resource-settings endpoints), `jobs/maintenance.py` (`maintain_clickhouse` → `maintain_telemetry`: checkpoint, raw_line expiry, audit/security 730-day delete, stats log), `worker.py` cron
9. `health.py`, `api/routes/support.py`, `startup.py` (`init_clickhouse` → `verify_telemetry()`: health + schema version check; retention days now applied by the nightly job, not DDL)
10. `scripts/backfill_harness_telemetry.py`

### Step 6 — migration tooling
* `observal_shared/migration/ch_export.py` → `legacy_clickhouse_export.py` (rename only; adjust `__init__.py`).
* New `observal_shared/migration/duckdb_import.py`, `duckdb_export.py` (calls `/v1/export`), `cutover.py` (orchestrates §4.2 with `state.json`), `verify.py` (digests, spot checks), `reverse.py`.
* `observal_shared/migration/validation.py::validate_ch` → `validate_telemetry`; `telemetry_manifest.py` unchanged format (v2.0) + `content_sha256` field.
* `observal-server/jobs/migration.py`: export/import/validate use DuckDB functions; new `run_telemetry_cutover_job`.
* `observal-server/models/migration_job.py` + `alembic/versions/027_telemetry_migration_scope.py`: enum `migration_scope` value `clickhouse` → `telemetry`; `operation_type` gains `telemetry_cutover`.
* `api/routes/admin/migrate.py`: scope names; new `POST /admin/migrate/telemetry-cutover`, `GET …/status`.
* `observal_cli/cmd_migrate.py`: `export-telemetry/import-telemetry/validate-telemetry` target DuckDB; new `telemetry-cutover` (`--clickhouse-url`, `--resume`, `--verify-only`, `--reverse`, `--json`), `observal server retire-clickhouse [--delete-volume]`.
* Web: `web/src/lib/types/admin.ts` (`MigrationScope = "postgres" | "telemetry" | "both"`, health `telemetry: boolean`), `migrate-form-fields.tsx`, `migrate-export-form.tsx`, admin dashboard backfill banner component, `settings.tsx` (remove resource card), `traces/index.tsx` comment only.
* Tests: `test_migrate*.py`, `test_migration_*.py`, `observal_cli/tests/test_cmd_migrate.py`, `test_migration_frontend.py`, A-08/A-09/A-19.

### Step 7 — deployment surfaces (§5)
* `docker/` (compose ×4, `entrypoint.sh`, `Makefile`), `docker/server-package/` (compose ×2, `env.template`, `setup.sh`), `grafana/` (datasource + 8 dashboards), `infra/helm/observal/` (new templates, values, NOTES, README), `infra/terraform/{aws,aws-standard,aws-ec2,gcp,azure}/`, `scripts/check_terraform_consistency.py`.
* Embedded: `observal_cli/server/{constants,deps,config_gen,orchestrator,backup,updater}.py`, `observal_cli/cmd_server.py`, `observal_cli/cmd_support.py`.
* Tests: `test_server_{backup,deps,orchestrator,upgrade}.py`, `test_cmd_support.py`, `test_cmd_logs.py`, `test_secret_files.py`, helm goldens, terraform validate CI, A-21/A-22.

### Step 8 — delete ClickHouse
* Remove `observal-server/services/clickhouse/`, `observal-server/clickhouse/`, `config.py` `CLICKHOUSE_*`, `tests/test_clickhouse_*.py`, `test_scope_cleanup_migrations.py`, `docker/clickhouse/` (moved under `docker/legacy-clickhouse/` used only by the profile), `tools/release.py` path reference, `pyproject.toml` B608 comment.
* Add A-15, A-16, A-17 lint tests.

### Step 9 — docs, skills, changelog
* `docs/self-hosting/*` (databases, data-migration → cutover runbook, backup-and-restore, resource-tuning, ports-and-volumes, requirements, troubleshooting, upgrades, docker-compose, kubernetes-helm, aws/gcp terraform, production/single-node deploy), `docs/reference/environment-variables.md`, `docs/reference/api-endpoints.md`, `docs/cli/{migrate,server,admin,support}.md`, `docs/core-concepts/session-tracking.md`, `docs/self-observability.md`, `docs/security/assurance-case.md`, `README.md`, `SETUP.md`, `AGENTS.md` (architecture + DB sections), `CONTRIBUTING.md`, `ROADMAP.md`, `observal-server/README.md`, `fuzz/README.md`, skills `observal_cli/skills/observal-admin/references/server-operations.md`, `observal_cli/skills/observal/references/commands.md`, `CHANGELOG.md` (breaking: ClickHouse Cloud mode removed; week bucketing Monday; resource settings removed).
* New `docs/self-hosting/telemetry-service.md` (§2, §6).

### Step 10 — evidence
* Attach perf results (A-20), cutover run log (A-19/A-22), Grafana + UI screenshots (A-23/A-24) to the PR per `AI_POLICY.md`.

---

## 9. Known traps (lessons from #1743, restated as rules)

| Trap | Rule in this plan | Enforced by |
|---|---|---|
| `INSERT OR REPLACE` | Requires a PK (ART index) and hits DuckDB's over-eager constraint checking when the same key is touched twice in one transaction; replace is `DELETE … IN keys; INSERT` on the single writer. | A-17 lint; `writer.py` is the only place that writes |
| ART indexes / `PRIMARY KEY` / `UNIQUE` / `CREATE INDEX` | Multiplies memory and WAL replay time at 30 M rows, slows bulk import, and offers nothing over integer column scans + zone maps. Identity lookups use `session_key BIGINT`. | A-17 lint; perf suite proves targets without indexes |
| Intermediate migration churn | One `001_baseline.sql`; schema changes during the branch edit the baseline, never add files; cutover imports directly into the final schema; derived tables rebuilt, not imported. | A-17 (single file), review |
| Silent query failures | Client raises; routes never `return []` on error; 503/504/413 surfaced; health degraded; audit sink drops are counted. | A-15 AST lint; A-13 stop-fixture test |
| Interactive timeout on exports | Export/import/backup/rebuild are jobs with heartbeat; HTTP calls that start them return immediately; CLI polls; only "no progress for 30 min" aborts. | A-11, A-19 (killed-and-resumed import) |
| Multiple writers | Service refuses `--workers > 1`; file lock on the `.duckdb`; API/worker never import `duckdb`. | A-07; import-lint in A-16 (`import duckdb` allowed only under `telemetry_store/`) |
| Overwriting live data during backfill | Chunk import only replaces rows with `ingested_at <= chunk max`; checkpoints never regress. | A-09, A-10 |
| Deleting the old volume | `chdata` stays declared; deletion is an explicit, confirmed command. | A-22 |
| Sentinel timestamps and offsets | Keep `_normalize_ts` clamping and the `1971 < ts < 2099` filters; keep `0xFFFFFFFF` credits row (`UBIGINT` column). | A-02, A-14 |
| Dialect drift | `toStartOfWeek` Sunday→Monday is the only accepted semantic change; everything else must match the recorded parity snapshots. | A-14 |
| Dropping `raw_line` TTL | Reimplemented as nightly expiry with `raw_line_truncated=2`. | A-12 |
| String-interpolated SQL | `project_id = '{project_id}'` replace hack and f-string `LIMIT` removed; named `$params` everywhere; the service rejects non-read statements on `/v1/query`. | A-03, review |
