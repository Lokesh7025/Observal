<!-- SPDX-FileCopyrightText: 2026 Observal Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Server operations

## Contents

- Service lifecycle
- Upgrade and rollback
- PostgreSQL migration
- Telemetry migration and ClickHouse cutover
- Safety checks

Local server commands use shell, filesystem, Docker, and database authority. API roles do not constrain that local authority.

## Service lifecycle

```bash
observal server status --output json
observal server start --background --output json
observal server restart --background --output json
observal server logs api --lines 100 --output json
observal server stop --output json
```

JSON start and restart require background mode. Verify final service status rather than trusting the launch response alone.

## Upgrade and rollback

Read current and available versions first:

```bash
observal server versions --output json
observal server upgrade --dry-run --output json
```

Execute only the requested direction:

```bash
observal server upgrade --version VERSION --force --output json
observal server rollback --force --output json
```

Rollback restores PostgreSQL and managed Docker image state, not the telemetry store. Verify service status and version after completion.

## PostgreSQL migration

Export, validate, then import:

```bash
observal server migrate export --file registry.tar.gz --output json
observal server migrate validate --archive registry.tar.gz --output json
observal server migrate import --archive registry.tar.gz --output json
```

Source commands read `DATABASE_URL`; target commands read `TARGET_DATABASE_URL`. Keep URLs out of output and logs. Never replace these commands with hand-written SQL.

## Telemetry migration

```bash
observal server migrate export-telemetry --telemetry-url http://localhost:8125 --output-dir telemetry-export --output json
observal server migrate validate-telemetry --input-dir telemetry-export --output json
observal server migrate import-telemetry --telemetry-url http://localhost:8125 --input-dir telemetry-export --output json
```

Commands read `TELEMETRY_URL` and `TELEMETRY_TOKEN`. Export requires an empty destination directory. Import is idempotent and rebuilds session summaries afterwards. Validate files and Registry references before import.

## ClickHouse cutover (existing installs)

Upgraded servers write to the DuckDB telemetry store immediately; ClickHouse history is backfilled with:

```bash
observal server migrate telemetry-cutover --clickhouse-url clickhouse://default:PASSWORD@localhost:8123/observal \
  --telemetry-url http://localhost:8125 --artifact-dir ./cutover --output json
observal server migrate telemetry-cutover ... --artifact-dir ./cutover --resume     # continue after an interruption
observal server migrate telemetry-cutover ... --artifact-dir ./cutover --verify-only
observal server retire-clickhouse --output json                                     # stop ClickHouse, keep its volume
```

Nothing is deleted on either side. Run `retire-clickhouse --delete-volume` only after verification and an explicit operator confirmation.

## Safety checks

- Confirm source, destination, and backup location before import, upgrade, rollback, or reset.
- Use dry run when available.
- Stop after validation failure. Do not import a damaged archive.
- Report counts, versions, warnings, and final service health.
- Never expose database URLs, generated secrets, archive contents, or customer rows.
