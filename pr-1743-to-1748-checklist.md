# PR #1743 → PR #1748 hardening checklist

This checklist treats PR #1748 as the implementation base and PR #1743 as a source of production-hardening requirements and regression tests.

Reviewed heads:

- PR #1743: `4abcc5708859d72531aa8889506c3a4222219064`
- PR #1748: `6e9b3da1fbde41047f945a7699e131ca1fe15fda`
- Common base: `594dd20ceb5a40821650f6faa6a18e790941c250`

## Rules for using this checklist

- Do not merge or cherry-pick PR #1743 wholesale. Port behavior into PR #1748's `telemetry_store` and `services.telemetry` architecture.
- Add a regression test in PR #1748 for every behavior ported from PR #1743.
- Mark an item complete only when the implementation, automated test, and relevant deployment documentation are all present.
- Validate the final `main...HEAD` diff. Passing tests inherited from either PR are not proof that the combined behavior is correct.

## P0 — required before merge

### 1. Long-session reads

Source: `ea133b52` (`fix(api): stop reporting unreadable sessions as empty`)

Current PR #1748 gap: `api/routes/sessions.py` issues one unbounded main-session query and one unbounded subagent query, while the telemetry store enforces `TELEMETRY_MAX_RESULT_ROWS=200000` by default.

- [ ] Port stable keyset pagination for main-session events.
- [ ] Port stable keyset pagination for subagent events using `(session_id, line_offset)`.
- [ ] Keep every page below the telemetry result-row cap.
- [ ] Preserve deterministic event ordering across page boundaries.
- [ ] Ensure a telemetry outage or result-limit failure becomes an explicit API error, never a valid empty session.
- [ ] Test sessions below, at, and above the configured result cap.
- [ ] Test a parent session whose combined subagent events exceed the result cap.
- [ ] Test incremental `after_offset` reads across multiple pages.

Evidence required:

- API test with more than 200,000 events.
- Store-unavailable test returning the documented telemetry error.
- No skipped or duplicated events at page boundaries.

### 2. Same-batch replacement deduplication

Sources: `1e2f2be5`, `7955fdd9`

Current PR #1748 gap: `telemetry_store.writer.replace_rows()` deletes existing keys and inserts every incoming row. Duplicate logical keys inside one request remain duplicated because the schema intentionally has no PK/UNIQUE constraint.

- [ ] Add an incoming-order column to staged replacement batches.
- [ ] Deduplicate staged rows by the complete logical key before delete and insert.
- [ ] Define and enforce last-payload-wins semantics.
- [ ] Apply the same semantics to session events and layer snapshots.
- [ ] Confirm checkpoint and summary replacement cannot produce duplicate logical rows.
- [ ] Test duplicate keys inside one request.
- [ ] Test duplicate keys across retries/replays.
- [ ] Test duplicate sentinel/non-source event rows.
- [ ] Test concurrent reads during repeated replacement.

Evidence required:

- Exactly one stored row per logical key after each test.
- The retained row is the final incoming payload.
- No DuckDB index or constraint is introduced.

### 3. Backup restoration and rollback consistency

Sources: `71e0e7a4`, `3f897885`, `b03004c1`, `87c6d7b4`, `a84d34e5`

Current PR #1748 gap: upgrade backup creates `telemetry.duckdb`, but telemetry snapshot failures are non-critical and `restore_backup()` restores only PostgreSQL. `server rollback` explicitly reports `telemetry_restored: false`.

- [ ] Make telemetry backup failure fatal when DuckDB is the active telemetry store.
- [ ] Verify the backup exists, is non-trivial, opens successfully, and has the expected schema before upgrade continues.
- [ ] Restore the DuckDB backup together with PostgreSQL for a same-generation rollback.
- [ ] Stop the telemetry service before replacing its database file.
- [ ] Preserve file ownership and permissions after restore.
- [ ] Restart the service and verify schema, health, and representative row counts.
- [ ] Make partial restore failure explicit and recoverable; never report rollback success with mismatched stores.
- [ ] Retain both ClickHouse and DuckDB volumes until explicit retirement.
- [ ] Prevent rollback to a pre-DuckDB release after a completed cutover unless a tested reverse migration was performed.
- [ ] Test rollback failure at each transition: before stop, after stop, after file replacement, after PostgreSQL restore, and during health verification.

Evidence required:

- Destructive compose test proving PostgreSQL and telemetry both return to the same backup point.
- Test proving an incompatible pre-DuckDB rollback is rejected before any data is restored.

### 4. Upgrade and ClickHouse cutover state machine

Sources: `77c53e76`, `39333b52`, `87c6d7b4`, `a84d34e5`

Current PR #1748 gap: server-package setup starts the DuckDB stack, reports success, and only then prints manual backfill instructions. Historical telemetry is unavailable until the operator notices and completes the separate command.

Decision required:

- [ ] Choose and document one release contract:
  - automatic cutover during `observal server upgrade`; or
  - an explicit two-stage upgrade that cannot silently finalize with pending legacy telemetry.

State-machine requirements:

- [ ] Detect legacy ClickHouse configuration, container, data, and volume without deleting any of them.
- [ ] Provision and persist the telemetry token without rotating an existing value.
- [ ] Start DuckDB alongside legacy ClickHouse.
- [ ] Record durable cutover state and make every phase retry-safe.
- [ ] Export ClickHouse in resumable, checksummed chunks.
- [ ] Import idempotently without overwriting rows written live after the export cutoff.
- [ ] Rebuild summaries and checkpoints without regressing live checkpoints.
- [ ] Verify source counts, imported counts, checksums, and representative session contents.
- [ ] Health-gate the new API after migration.
- [ ] Keep ClickHouse data after successful cutover; retirement/deletion remains explicit.
- [ ] Define recovery for failure before export, during export, during import, during rebuild, during verification, and after deployment.
- [ ] Ensure `--resume` validates existing artifacts rather than merely trusting phase timestamps.
- [ ] Ensure completed cutovers are not accidentally repeated or rolled back into the legacy topology.

Deployment coverage:

- [ ] Server-package Compose
- [ ] Source Compose
- [ ] Embedded server
- [ ] Helm
- [ ] AWS Terraform
- [ ] AWS Standard Terraform
- [ ] AWS EC2 Terraform
- [ ] GCP Terraform
- [ ] Azure Terraform

Evidence required:

- Failure-injection tests for every state transition.
- One live migration from the last released ClickHouse topology.
- A second run that proves the operation is a no-op or safe resume.

### 5. Snapshot-consistent, scalable DuckDB export

Source: `7a0db1c3`

Current PR #1748 gap: `telemetry_store.jobs.start_export()` performs a count and then repeated `ORDER BY ... LIMIT ... OFFSET ...` queries while writes may continue. This repeatedly scans/sorts prior rows and can skip or duplicate rows if the table changes between chunks.

- [ ] Replace `LIMIT/OFFSET` export pagination.
- [ ] Export every table from one consistent snapshot.
- [ ] Prefer one bounded/single-pass `COPY` per table or stable keyset chunking inside a snapshot transaction.
- [ ] Ensure export work is not subject to the interactive query timeout.
- [ ] Verify emitted chunk counts sum exactly to the manifest count.
- [ ] Preserve NULL timestamps and all declared columns.
- [ ] Keep output names independent of caller-controlled values.
- [ ] Test concurrent ingestion during export.
- [ ] Test multi-year datasets and many chunks.
- [ ] Test export/import round-trip at multi-million-row scale.

Evidence required:

- No skipped or duplicated logical identities under concurrent writes.
- Runtime does not grow quadratically with chunk number/history depth.

### 6. Import ledger and manifest drift protection

Sources: `3f897885`, `b03004c1`, `5f2358ac`

Current PR #1748 gap: an existing `(migration_id, chunk_id)` is treated as complete without comparing its stored table or SHA-256 to the new request. Local `import_state.json` also records row count but not artifact identity.

- [ ] On an existing ledger entry, compare table, SHA-256, and expected row count.
- [ ] Reject reused chunk IDs whose artifact identity changed.
- [ ] Store and compare SHA-256 in local resume state.
- [ ] Validate the complete manifest before importing the first chunk.
- [ ] Reject unknown tables, duplicate chunk IDs, traversal paths, missing files, extra unexpected files where applicable, and row-count inconsistencies.
- [ ] Keep empty-table handling explicit and idempotent.
- [ ] Validate migration IDs and chunk IDs before using them in state or filenames.
- [ ] Test interrupted import followed by a valid resume.
- [ ] Test interrupted import followed by a modified chunk/manifest.

Evidence required:

- Modified resumed artifacts fail closed.
- A valid retry performs no duplicate writes.

### 7. Server-owned paths and bounded uploads

Sources: `8a306530`, `539fd2e0`, `4abcc570`

Current PR #1748 gaps:

- `/v1/export` accepts an arbitrary `dest_dir`.
- `/v1/admin/backup` accepts an arbitrary `dest_path`.
- Backup paths are interpolated into `ATTACH` SQL.
- `/v1/import/chunk` streams until EOF without a service-level byte limit.

- [ ] Remove arbitrary filesystem destinations from the HTTP API.
- [ ] Generate export/backup destinations beneath configured server-owned roots.
- [ ] Return opaque job/file handles to callers.
- [ ] Verify all resolved paths remain inside the configured root.
- [ ] Ensure request-derived table names and path components never reach filesystem operations.
- [ ] Avoid direct path interpolation in SQL; escape safely where DuckDB requires a literal.
- [ ] Add a configurable maximum import chunk size.
- [ ] Enforce the limit while streaming and return 413.
- [ ] Check free space before accepting/importing large artifacts.
- [ ] Clean partial files after disconnects, cancellation, checksum failure, and oversized upload rejection.

Evidence required:

- Traversal, symlink, SQL-literal, oversized upload, and interrupted-upload tests.

### 8. Query endpoint sandbox

Related hardening area from `15ffa645`, `539fd2e0`, and `4abcc570`.

Current PR #1748 gap: read-only enforcement uses a small substring deny-list. DuckDB exposes additional filesystem/network table functions and aliases such as `read_text` and `parquet_scan`.

- [ ] Restrict queries to approved telemetry schemas/tables.
- [ ] Reject table functions and external scans structurally rather than relying only on a string deny-list.
- [ ] Disable DuckDB external access and extension installation/loading where supported.
- [ ] Reject filesystem reads, URL reads, secrets access, `ATTACH`, `COPY`, extension operations, and multi-statements.
- [ ] Keep Grafana queries working through the same restricted contract.
- [ ] Ensure parser/binder errors do not disclose sensitive internal paths or configuration.

Evidence required:

- Tests covering known aliases and bypass forms, comments, casing, quoting, CTEs, macros, and nested subqueries.

## P1 — required for production readiness

### 9. Telemetry-only dependency and image packaging

Source: `afc184cd`

Current PR #1748 gap: `duckdb`, `filelock`, `prometheus-client`, and `pyarrow` are base server dependencies, and the telemetry service uses the API image.

- [ ] Decide explicitly whether one shared image is an accepted deployment tradeoff.
- [ ] Prefer a telemetry optional dependency group/build target.
- [ ] Keep the DuckDB engine out of API, worker, and init images unless technically required.
- [ ] Ensure the telemetry image contains all runtime files, migrations, timezone support, and backup tools.
- [ ] Verify API and worker can import their telemetry HTTP client without importing DuckDB.
- [ ] Build and boot both images in CI.

Evidence required:

- `import duckdb` fails in the API/worker image if image separation is selected.
- Telemetry image boots, migrates, ingests, queries, exports, and backs up.

### 10. Migration checksum and schema drift detection

Sources: `18990490`, `198af858`

PR #1748 already has one no-index baseline, but `schema_migrations` records only version/name and does not verify a checksum for previously applied files.

- [ ] Keep exactly one final baseline for the first release.
- [ ] Keep telemetry tables free of PK, UNIQUE, and ART indexes.
- [ ] Record normalized migration checksums.
- [ ] Refuse startup when an already-applied released migration changes.
- [ ] Do not add compatibility code for unreleased intermediate #1743/#1748 schemas.
- [ ] Test fresh creation, subsequent additive migration, and changed-file rejection.

### 11. Writer pause and administrative operation semantics

Related source: `198af858` plus #1743 cutover work.

- [ ] Make pause linearizable: once pause reports success, no previously queued normal write may commit unnoticed.
- [ ] Define whether queued writers wait, fail, or drain.
- [ ] Keep backup/checkpoint/rebuild operations serialized through the writer owner.
- [ ] Prevent conflicting administrative jobs from running simultaneously where they can invalidate one another.
- [ ] Persist or reconstruct enough job state to resume safely after service restart.
- [ ] Test pause during queued and in-flight writes.

### 12. Canonical identity safety

PR #1748 improvement to retain, with additional validation.

- [ ] Keep `session_key`/`snapshot_key` as scan accelerators, not the sole correctness identity.
- [ ] Include canonical identity columns when deleting/replacing or explicitly document and accept 64-bit collision semantics.
- [ ] Validate required identity columns before hashing; do not silently hash missing values as empty strings.
- [ ] Test cross-project, cross-user, cross-harness, and parent-session isolation.
- [ ] Test a mocked hash collision if canonical columns remain part of correctness checks.

### 13. Backup and export operational controls

Sources: `3f897885`, `7a0db1c3`, `198af858`

- [ ] Set retention/cleanup rules for server-generated exports, temporary uploads, and snapshots.
- [ ] Prevent concurrent jobs from exhausting disk.
- [ ] Emit progress heartbeats during every long phase, including checksum generation and file verification.
- [ ] Verify backup integrity, not only file size and SHA-256.
- [ ] Document and test restoration for Docker, embedded, Helm, and each Terraform deployment.

### 14. Deployment state and secret preservation

Sources: `71e0e7a4`, `b03004c1`, `15ffa645`, `87c6d7b4`

- [ ] Verify Terraform `moved` blocks against existing released state for every cloud module.
- [ ] Confirm disks, instances, identities, DNS, security groups, and object-storage backups are preserved.
- [ ] Confirm existing telemetry tokens are not rotated on upgrade.
- [ ] Verify secret files are readable by the actual runtime UID/GID in release images.
- [ ] Confirm health checks use authentication where required and intentionally omit it only for public health endpoints.
- [ ] Test a fresh named volume and an existing upgraded volume.
- [ ] Validate memory limits at both container and DuckDB levels.

## P2 — release completeness and cleanup

### 15. Source Compose image behavior

Source: `b543dc14`

- [ ] Document whether source Compose builds local tags or pulls release images.
- [ ] Ensure `make up`, `make rebuild`, and documented commands match that choice.
- [ ] Keep server-package Compose as the released-image topology.

### 16. Grafana behavior

Source: `869abaca` identified the need to account for removed dashboards. PR #1748 replaces rather than deletes them.

- [ ] Keep the replacement Infinity datasource token server-side.
- [ ] Execute every generated dashboard query against the final schema in tests.
- [ ] Validate Grafana provisioning against a live stack.
- [ ] Document any panel or time-bucketing semantic differences.
- [ ] Confirm ordinary Grafana users cannot use the datasource to bypass telemetry query restrictions.

### 17. Scale limits and documentation

Source: `06447e02`

- [ ] Retain the 30M-row benchmark evidence from PR #1748.
- [ ] Add write/replay measurements for duplicate replacement batches.
- [ ] Measure export with many chunks and concurrent ingest.
- [ ] Document the tested write-rate, read-concurrency, memory, storage, and retention ceilings.
- [ ] State when a single-node DuckDB deployment is no longer appropriate.

### 18. Small correctness and maintenance items

Source: `198af858`

Review each item against PR #1748's different architecture:

- [ ] All connection-local settings apply to every reader cursor/connection.
- [ ] Invalid settings are rejected rather than reported as applied.
- [ ] No code reaches into private queue internals.
- [ ] DML/result accounting is truthful for typed operations.
- [ ] No unused environment/configuration values remain.
- [ ] Embedded startup applies schema exactly once.
- [ ] Orphaned legacy ClickHouse data is reported clearly.
- [ ] Temporary ClickHouse used for migration binds only to the intended interface and has an explicit credential policy.
- [ ] Logs use Loguru positional formatting and never expose tokens, URLs with credentials, or artifact contents.

### 19. Scope and attribution cleanup

Sources: `6c4e9256`, `7bfdcd6d`

- [ ] Keep unrelated SPDX-hook changes out of the telemetry PR.
- [ ] Credit actual authors according to repository convention.
- [ ] Remove files and compatibility paths that exist only for unreleased intermediate implementations.
- [ ] Update the PR's AI-assistance disclosure accurately.
- [ ] Include screenshots for migration UI changes or explicitly remove the UI scope.

### 20. Release tooling and changelog

Source: `869abaca`

- [ ] Ensure release tooling preserves and correctly folds the `Unreleased` section.
- [ ] Add a breaking-change entry for ClickHouse removal and the required migration sequence.
- [ ] Document Grafana changes, ports, volumes, environment variables, backup/restore, and rollback boundaries.
- [ ] Regenerate bundled CLI skill references after command changes.
- [ ] Test generated command references for drift.

## Already reflected in PR #1748 — retain and regression-test

These #1743 lessons are already represented architecturally in PR #1748 and should not be lost while hardening:

- [x] One final baseline schema rather than unreleased migration churn.
- [x] No telemetry PK, UNIQUE constraint, ART index, or `INSERT OR REPLACE` path.
- [x] A single process owns the DuckDB file and refuses a second owner.
- [x] Writes are serialized through one writer executor.
- [x] Session events, summary refresh, and checkpoint advance share one transaction.
- [x] Interactive reads have timeout, row-count, queue, and concurrency bounds.
- [x] Telemetry client failures raise instead of returning fallback empty data.
- [x] Long-running export, rebuild, and backup operations are represented as jobs rather than interactive queries.
- [x] Imports are checksummed and ledger-backed at a basic level.
- [x] Derived summaries/checkpoints are rebuilt rather than imported as authoritative state.
- [x] Live post-cutoff rows are intended to survive legacy backfill.
- [x] ClickHouse data deletion is an explicit retirement action.
- [x] Grafana dashboards are replaced rather than silently removed.
- [x] Real in-process DuckDB integration tests and architectural policy tests exist.
- [x] A 30M-row benchmark harness and recorded results exist.

## Explicitly do not port from PR #1743

- [x] Do not port the generic arbitrary write/execute API; keep PR #1748's typed write endpoints.
- [x] Do not port PK/index migration history from unreleased intermediate schemas.
- [x] Do not port compatibility shims for unreleased DuckDB databases.
- [x] Do not port the composite-string-only hot read path; retain PR #1748's integer accelerator.
- [x] Do not delete working Grafana dashboards merely because the old ClickHouse datasource is removed.
- [x] Do not reintroduce unrelated SPDX hook or Playwright-ignore changes.

## Final acceptance gate

- [ ] Every P0 item has implementation, regression tests, and operational documentation.
- [ ] Every P1 item is complete or explicitly accepted with a written rationale.
- [ ] Full Python, CLI, web, Helm, Terraform, REUSE, dependency, and CodeQL CI passes.
- [ ] Source Compose and release Compose pass live ingest/query/export/backup/restore tests.
- [ ] A real last-release ClickHouse deployment successfully upgrades and migrates.
- [ ] A forced failure at every cutover phase is recoverable without deleting either store.
- [ ] A same-generation rollback restores PostgreSQL and DuckDB to one consistent point.
- [ ] A pre-DuckDB rollback after completed cutover is rejected or follows a tested reverse-migration procedure.
- [ ] An independent reviewer audits the final `main...HEAD` diff and finds no P0/P1 defects.
