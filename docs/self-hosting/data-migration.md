<!-- SPDX-FileCopyrightText: 2026 Hari Srinivasan <harisrini21@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Data migration

Use data migration when you need to move an Observal instance to a new deployment, validate a backup, or copy production data into a controlled recovery environment.

Only super admins can start migration jobs.

## What can be moved

- **Registry data**: users, agents, components, versions, settings, review records, and related PostgreSQL data.
- **Telemetry data**: sessions, layer snapshots, audit events, security events, and webhook delivery history stored in the telemetry store. Tables are exported as checksummed Parquet chunks by the store itself, so total export size is not bounded by request memory or HTTP timeouts. Session summaries and checkpoints are rebuilt on import rather than copied.
- **Registry + telemetry**: a full instance move when both stores are available.

## Before you start

1. Confirm both source and target instances are on compatible Observal versions.
2. Schedule a maintenance window if users are actively changing registry data.
3. Make sure the target deployment has enough disk space for uploaded artifacts.
4. Decide whether you need registry data only or registry plus telemetry.
5. Treat exported files like production backups. They can contain hashed credentials, API keys, and telemetry with PII.

## Export from the source instance

1. Open **Admin → Settings → Data Migration**.
2. Click **Migrate**.
3. Select **Export**.
4. Choose the export scope:
   - **Registry data** for PostgreSQL records only.
   - **Registry + telemetry** for a full move.
5. Click **Start export**.
6. Wait for the job to finish.
7. Download every artifact shown in the result.
8. Store the artifacts in a secure temporary location.

## Validate before import

Run validation on the target instance before importing.

1. Open **Admin → Settings → Data Migration** on the target instance.
2. Select **Validate**.
3. Upload the artifacts from the export. For **Registry + telemetry**, add both `pg_export.tar.gz` and `telemetry_export.tar.gz`; the picker retains files added in separate selections and lists each selected artifact.
4. Choose the same scope you plan to import.
5. Click **Start validation**.
6. Review the result:
   - Checksums should pass.
   - Table counts should match expectations.
   - Telemetry validation should not report broken registry references unless you intentionally skipped registry data.

Do not import artifacts that fail checksum validation.

## Import into the target instance

1. Open **Admin → Settings → Data Migration** on the target instance.
2. Select **Import**.
3. Upload the validated artifacts. For **Registry + telemetry**, both the PostgreSQL and telemetry archives are required.
4. Choose the import scope.
5. Imports are idempotent: every chunk is recorded in the store's import ledger, so a retried or resumed import never duplicates rows. Summaries and checkpoints are rebuilt after the last chunk.
6. Click **Start import**.
7. Wait for the job to finish.
8. Check agents, components, users, and sessions in the target instance.

PostgreSQL imports skip conflicting rows. Telemetry imports resume per checksummed Parquet chunk; a retry skips only chunks already completed for the same migration artifact.

## CLI alternative

The CLI uses the same shared migration core as the server jobs. Registry commands read `DATABASE_URL` (source) and `TARGET_DATABASE_URL` (target); telemetry commands read `TELEMETRY_URL` and `TELEMETRY_TOKEN`.

```bash
observal server migrate export --file backup.tar.gz --output json
observal server migrate validate --archive backup.tar.gz --output json
observal server migrate import --archive backup.tar.gz --output json
```

Telemetry commands are separate:

```bash
observal server migrate export-telemetry --telemetry-url http://source:8125 --output-dir telemetry --output json
observal server migrate validate-telemetry --input-dir telemetry --telemetry-url http://target:8125 --output json
observal server migrate import-telemetry --telemetry-url http://target:8125 --input-dir telemetry --output json
```

## Migrating an existing ClickHouse install (cutover)

Releases before the DuckDB telemetry store kept telemetry in ClickHouse. Upgrading is a **switch-then-backfill**: the new version writes to the telemetry store from the first request, and you copy ClickHouse history into it afterwards while ingest keeps running. Nothing is deleted on either side until you say so.

1. **Upgrade** the stack. Keep ClickHouse running next to the new telemetry service:
   - Compose / server package: `docker compose --profile legacy-clickhouse up -d` (the `chdata` volume is still declared).
   - Helm: `--set clickhouse.legacy.enabled=true`.
   - Terraform (AWS, GCP, Azure): `enable_legacy_clickhouse = true`.
   - Embedded (`observal server`): nothing to do; the CLI starts the old data directory for you.
2. **Run the cutover** (resumable, verifies before it reports success):

   ```bash
   observal server migrate telemetry-cutover \
     --clickhouse-url clickhouse://default:PASSWORD@localhost:8123/observal \
     --telemetry-url http://localhost:8125 --telemetry-token "$TELEMETRY_TOKEN" \
     --artifact-dir ./cutover --output json
   ```

   Steps: preflight (both stores reachable, source counts, disk space) → chunked, checksummed ClickHouse export → idempotent import → rebuild of session summaries and checkpoints → verification (per-table row counts against the export cutoff plus a 100-session event-by-event spot check). Rows ingested live after the cutoff are never overwritten by the backfill. Re-run with `--resume` after an interruption, or `--verify-only` to re-check later.
3. **Retire ClickHouse** once verification passes: `observal server retire-clickhouse` (embedded), `docker compose --profile legacy-clickhouse stop observal-clickhouse`, `clickhouse.legacy.enabled=false`, or `enable_legacy_clickhouse = false`. The ClickHouse volume stays until you delete it explicitly (`retire-clickhouse --delete-volume`, `docker volume rm`, PVC deletion).

### Rolling back

Roll back the image with `observal server rollback`; ClickHouse still holds everything up to the cutover, so the only gap is telemetry ingested into the store since then. To close it, export those rows and load them into ClickHouse:

```bash
observal server migrate telemetry-cutover --reverse --since "2026-06-01 00:00:00" \
  --clickhouse-url ... --telemetry-url ... --artifact-dir ./rollback
# then, per chunk: clickhouse-client --query "INSERT INTO <table> FORMAT Parquet" < chunk.parquet
```

`observal reconcile` fills any remaining gap from the local session JSONL files.

## Cleanup

1. Confirm the target instance works.
2. Delete local copies of migration artifacts.
3. Remove temporary upload files from the target host if you copied them outside the UI.
4. Keep only the backup copy required by your retention policy.

## Troubleshooting

### Validation fails

Confirm the selected scope matches the uploaded files. **Registry + telemetry** requires both export archives, and the picker must show both filenames before submission. Re-download the artifacts from the source export if checksums fail; if they still fail, create a new export.

Large migration uploads are streamed through nginx and spooled to the persistent migration data volume instead of API memory. Ensure that volume has room for both the uploaded telemetry archive and its extracted Parquet files during validation/import.

### Import resumes completed telemetry chunks

Telemetry resume state is stored beside the extracted artifact in `import_state.json` and, authoritatively, in the store's `telemetry_import_ledger` table. A retry verifies each artifact's checksum and skips chunks already imported for the same migration id.

### The cutover reports a verification failure

Nothing was deleted. Read `cutover_state.json` in the artifact directory: `count_mismatches` lists tables where the store holds fewer pre-cutoff rows than ClickHouse, and `spot_check.mismatched` lists sessions whose events differ. Re-run with `--resume` (chunks already in the ledger are skipped) and then `--verify-only`.

### ClickHouse reports a memory limit during the cutover export

The exporter automatically subdivides a memory-limited chunk and retries it. If the smallest supported chunk still fails, inspect the reported table and chunk ID for a pathological key distribution.

### Telemetry import has missing registry references

Import registry data first, then validate and import telemetry again.

### Jobs time out

Increase the migration job timeout setting or split registry and telemetry into separate operations.
