<!-- SPDX-FileCopyrightText: 2026 Naraen Rammoorthi <naraen13@gmail.com> -->
<!-- SPDX-FileCopyrightText: 2026 Observal Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# `observal server migrate`

Move PostgreSQL registry data and telemetry between Observal deployments, and back-fill a DuckDB telemetry store from a legacy ClickHouse install.

Migration uses the supplied database connections directly. Local shell and database access are the authorization boundary; the command does not authenticate against a configured Observal API.

Install the optional dependency first:

```bash
pip install 'observal-cli[migrate]'
```

Keep connection URLs in environment variables or secret files managed by the shell. Registry commands read `DATABASE_URL` (source) and `TARGET_DATABASE_URL` (target); telemetry commands read `TELEMETRY_URL` and `TELEMETRY_TOKEN`; the cutover additionally reads `CLICKHOUSE_URL`. Explicit URL options remain available when no secret is embedded. Do not paste credentials into shared shell history, logs, or issue reports. JSON results and categorized errors never echo a connection URL.

## Workflow

1. Export PostgreSQL. This creates a checksummed registry archive and a migration manifest.
2. Validate and import PostgreSQL on the target.
3. Export telemetry from the source telemetry store.
4. Validate and import telemetry on the target.

PostgreSQL must be imported first so referenced users and agents exist before telemetry validation.

## PostgreSQL export

```bash
observal server migrate export \
  --file registry.tar.gz \
  --output json
```

`--file/-f` selects the archive destination. `--output/-o` always selects `table` or `json`. Existing destinations fail with a conflict instead of being overwritten.

The archive and sidecar manifest are written atomically with owner-only permissions. A partial archive is not published after failure.

Example JSON fields:

```json
{
  "archive": "registry.tar.gz",
  "manifest": "registry.manifest.json",
  "migration_id": "7b84e503-63af-4b89-a1cd-abf48f0452f3",
  "table_counts": {"users": 8, "agents": 21},
  "total_rows": 342,
  "size_bytes": 1048576,
  "duration_seconds": 2.4
}
```

## PostgreSQL validation and import

```bash
observal server migrate validate \
  --archive registry.tar.gz \
  --output json

observal server migrate import \
  --archive registry.tar.gz \
  --output json
```

Validation checks archive structure and SHA-256 checksums. When a target URL is provided, it also compares table row counts. Checksum failure returns a categorized validation error. Row-count differences remain explicit result data.

Import verifies checksums before insertion. Existing rows are skipped according to the migration service's idempotent import rules. The result contains per-table inserted and skipped counts plus warnings.

## Telemetry export

```bash
observal server migrate export-telemetry \
  --telemetry-url http://source:8125 \
  --output-dir telemetry-export \
  --output json
```

The destination must be empty. The export runs as a job inside the telemetry store (no interactive timeout); the CLI polls for progress, downloads every Parquet chunk, verifies each SHA-256, and writes `telemetry_manifest.json` (schema `3.0`) recording table row counts and per-chunk checksums. `--since` limits the export to rows written after a UTC timestamp, which is what the rollback path uses.

## Telemetry validation and import

```bash
observal server migrate validate-telemetry \
  --input-dir telemetry-export \
  --telemetry-url http://target:8125 \
  --output json

observal server migrate import-telemetry \
  --telemetry-url http://target:8125 \
  --input-dir telemetry-export \
  --output json
```

Telemetry validation checks:

* Parquet checksums and per-chunk row counts (both `3.0` store exports and `2.0` legacy ClickHouse exports)
* Manifest row counts against the target telemetry store when supplied
* Agent and user references against target PostgreSQL when supplied

Checksum failure is fatal. Row-count differences and orphan groups are returned explicitly.

Telemetry import is idempotent per chunk: the store records `(migration_id, chunk_id)` in `telemetry_import_ledger`, so a retry skips completed chunks even if `import_state.json` beside the artifact is lost. Only `session_events`, `layer_snapshots`, `audit_log`, `security_events`, and `webhook_deliveries` are imported; `session_stats_agg` and `session_checkpoints` are rebuilt from the imported events afterwards (`--no-rebuild` skips that step). Imported rows never overwrite rows the store ingested live after the export's cutoff.

## ClickHouse cutover

Existing installs that stored telemetry in ClickHouse back-fill the new store with one resumable command while ingest keeps running:

```bash
observal server migrate telemetry-cutover \
  --clickhouse-url clickhouse://default:PASSWORD@localhost:8123/observal \
  --telemetry-url http://localhost:8125 --telemetry-token "$TELEMETRY_TOKEN" \
  --artifact-dir ./cutover --output json
```

Phases: preflight → chunked ClickHouse export (`legacy_clickhouse_export`, checksummed Parquet) → idempotent import → derived rebuild → verification (row counts against the export cutoff and a `--spot-check N` session-by-session comparison, default 100). State lives in `cutover_state.json`; `--resume` continues after an interruption and `--verify-only` re-checks a finished cutover. `--reverse --since TS` exports store rows for loading back into ClickHouse on rollback. Nothing is deleted on either side; retire ClickHouse afterwards with `observal server retire-clickhouse`.

## Human and JSON behavior

All seven leaves accept `--output table|json`. Human mode renders progress and summaries. JSON mode is finite, prompt-free, suppresses progress and warnings from stdout, and returns one result document. Failures leave stdout empty and emit one categorized error to stderr.

Cleartext ClickHouse transport with credentials (cutover source) produces a human warning. JSON mode does not print a banner; operators should use `clickhouses://` for TLS.

## Exit codes

| Code | Category | Typical migration cause |
| --- | --- | --- |
| 2 | Usage | Missing required option or unsupported output mode |
| 4 | Permission | Destination or source path is not accessible |
| 5 | Not found | Archive, manifest, or input directory is missing |
| 6 | Conflict | Archive or telemetry destination already exists |
| 7 | Validation | Invalid archive, failed checksum, or missing phase prerequisite |
| 9 | Unavailable | Optional dependency, database, network, or migration service failure |

## Recommended sequence

```bash
# Source
observal server migrate export --file registry.tar.gz --output json
observal server migrate export-telemetry \
  --telemetry-url http://source:8125 \
  --output-dir telemetry-export \
  --output json

# Target
observal server migrate validate --archive registry.tar.gz --output json
observal server migrate import --archive registry.tar.gz --output json
observal server migrate validate-telemetry \
  --input-dir telemetry-export \
  --output json
observal server migrate import-telemetry \
  --telemetry-url http://target:8125 \
  --input-dir telemetry-export \
  --output json
```
