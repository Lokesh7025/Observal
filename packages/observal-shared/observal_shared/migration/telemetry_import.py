# SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Import checksummed Parquet chunks into the DuckDB telemetry store.

Accepts both manifest formats:

* ``2.0`` - written by the legacy ClickHouse exporter (cutover source).
* ``3.0`` - written by :mod:`telemetry_export` from a DuckDB store.

Each chunk is posted to ``/v1/import/chunk``. The store keeps an import ledger
keyed by ``(migration_id, chunk_id)`` so a retried or resumed run never
duplicates rows. Derived tables are rebuilt afterwards, never imported.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from loguru import logger as optic

from observal_shared.migration.archive import read_manifest, write_manifest
from observal_shared.migration.connections import TelemetryConnParams, connect_telemetry
from observal_shared.migration.exceptions import ArtifactValidationError, MigrationError
from observal_shared.migration.results import TelemetryImportResult
from observal_shared.migration.telemetry_manifest import validate_telemetry_manifest
from observal_shared.telemetry_tables import DERIVED_TABLES, IMPORTED_TABLES, TELEMETRY_TABLES

if TYPE_CHECKING:
    from observal_shared.migration.progress import ProgressReporter

MANIFEST_FILENAME = "telemetry_manifest.json"
IMPORT_STATE_FILENAME = "import_state.json"
CHUNK_RETRY_ATTEMPTS = 5
CHUNK_RETRY_BASE_SECONDS = 1.0
CHUNK_RETRY_MAX_SECONDS = 30.0
JOB_POLL_SECONDS = 2.0
JOB_STALL_SECONDS = 30 * 60
_SUPPORTED_MANIFEST_VERSIONS = {"2.0", "3.0"}
_MIGRATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_CHUNK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _manifest_version(manifest: dict[str, Any]) -> str:
    versions = {str(value) for key in ("schema_version", "telemetry_manifest_version") if (value := manifest.get(key))}
    if len(versions) != 1 or not versions <= _SUPPORTED_MANIFEST_VERSIONS:
        raise ArtifactValidationError(f"unsupported or inconsistent telemetry manifest version: {sorted(versions)}")
    return next(iter(versions))


def _row_count(value: Any, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ArtifactValidationError(f"{context} has an invalid row_count")
    return value


def _safe_chunk_path(input_dir: Path, filename: Any, chunk_id: str) -> Path:
    if not isinstance(filename, str) or not filename or Path(filename).is_absolute():
        raise ArtifactValidationError(f"chunk {chunk_id} has an unsafe filename")
    root = input_dir.resolve()
    path = (input_dir / filename).resolve()
    if not path.is_relative_to(root):
        raise ArtifactValidationError(f"chunk {chunk_id} has an unsafe filename")
    return path


def validate_manifest(
    manifest: dict[str, Any], input_dir: Path, *, verify_artifacts: bool = False
) -> list[dict[str, Any]]:
    """Validate and normalize a v2 or v3 manifest under one canonical contract."""
    if not isinstance(manifest, dict):
        raise ArtifactValidationError("telemetry manifest must be a JSON object")
    version = _manifest_version(manifest)
    if version == "2.0":
        # Preserve all of the established ClickHouse export invariants: completed
        # phase, cutoff, complete table set, ranges, shards, IDs, and file lists.
        try:
            validate_telemetry_manifest(manifest, input_dir)
        except MigrationError as exc:
            raise ArtifactValidationError(str(exc)) from exc
    migration_id = manifest.get("migration_id")
    if not isinstance(migration_id, str) or not _MIGRATION_ID_RE.fullmatch(migration_id):
        raise ArtifactValidationError("telemetry manifest has an invalid migration_id")
    tables = manifest.get("tables")
    if not isinstance(tables, dict):
        raise ArtifactValidationError("telemetry manifest tables must be an object")
    if not all(isinstance(table, str) for table in tables):
        raise ArtifactValidationError("telemetry manifest table names must be strings")
    unknown_tables = sorted(set(tables) - set(TELEMETRY_TABLES))
    if unknown_tables:
        raise ArtifactValidationError(f"telemetry manifest contains unknown tables: {unknown_tables}")

    raw_chunks: list[tuple[str, dict[str, Any]]] = []
    if version == "3.0":
        chunks = manifest.get("chunks")
        if not isinstance(chunks, list):
            raise ArtifactValidationError("telemetry manifest chunks must be an array")
        for chunk in chunks:
            if not isinstance(chunk, dict) or not isinstance(chunk.get("table"), str):
                raise ArtifactValidationError("telemetry manifest contains an invalid chunk")
            raw_chunks.append((chunk["table"], chunk))
    else:
        for table, meta in tables.items():
            if not isinstance(meta, dict) or not isinstance(meta.get("chunks"), list):
                raise ArtifactValidationError(f"telemetry table {table} has invalid chunk metadata")
            raw_chunks.extend((table, chunk) for chunk in meta["chunks"])

    seen_ids: set[str] = set()
    seen_files: set[str] = set()
    totals = {table: 0 for table in tables}
    out: list[dict[str, Any]] = []
    for table, chunk in raw_chunks:
        if table not in TELEMETRY_TABLES or table not in tables:
            raise ArtifactValidationError(f"telemetry chunk references unknown or undeclared table: {table}")
        if not isinstance(chunk, dict):
            raise ArtifactValidationError(f"telemetry table {table} contains an invalid chunk")
        chunk_id = chunk.get("chunk_id")
        if not isinstance(chunk_id, str) or not _CHUNK_ID_RE.fullmatch(chunk_id) or chunk_id in seen_ids:
            detail = "missing or invalid" if not chunk_id or not isinstance(chunk_id, str) else f"duplicate: {chunk_id}"
            raise ArtifactValidationError(f"telemetry manifest chunk ID is {detail}")
        seen_ids.add(chunk_id)
        count = _row_count(chunk.get("row_count"), f"chunk {chunk_id}")
        totals[table] += count
        filename = chunk.get("file")
        if filename is None and version == "2.0" and count == 0:
            digest = chunk.get("sha256")
            if digest is not None and (not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest)):
                raise ArtifactValidationError(f"chunk {chunk_id} has an invalid SHA-256 digest")
            continue
        path = _safe_chunk_path(input_dir, filename, chunk_id)
        relative = str(path.relative_to(input_dir.resolve()))
        if relative in seen_files:
            raise ArtifactValidationError(f"telemetry manifest contains duplicate filename: {filename}")
        seen_files.add(relative)
        digest = chunk.get("sha256")
        if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
            raise ArtifactValidationError(f"chunk {chunk_id} has an invalid SHA-256 digest")
        if not path.is_file():
            raise ArtifactValidationError(f"required telemetry chunk file is missing: {filename}")
        if verify_artifacts:
            if _sha256(path) != digest:
                raise ArtifactValidationError(f"checksum mismatch for chunk {chunk_id}")
            try:
                import pyarrow.parquet as pq

                actual_rows = pq.read_metadata(path).num_rows
            except Exception as exc:
                raise ArtifactValidationError(f"chunk {chunk_id} is not valid Parquet") from exc
            if actual_rows != count:
                raise ArtifactValidationError(
                    f"row count mismatch for chunk {chunk_id}: expected {count}, found {actual_rows}"
                )
        out.append({"table": table, "chunk_id": chunk_id, "path": path, "sha256": digest, "row_count": count})

    for table, meta in tables.items():
        if not isinstance(meta, dict):
            raise ArtifactValidationError(f"telemetry table {table} has invalid metadata")
        expected = _row_count(meta.get("row_count"), f"table {table}")
        if totals[table] != expected:
            raise ArtifactValidationError(
                f"telemetry table {table} row total mismatch: expected {expected}, chunks contain {totals[table]}"
            )
        if version == "2.0":
            chunk_files = [chunk.get("file") for chunk in meta["chunks"] if chunk.get("file") is not None]
            if "files" in meta and meta["files"] != chunk_files:
                raise ArtifactValidationError(f"telemetry table {table} file list does not match its chunks")
            checksums = meta.get("checksum")
            if checksums is not None and checksums != {
                chunk["file"]: chunk["sha256"] for chunk in meta["chunks"] if chunk.get("file") is not None
            }:
                raise ArtifactValidationError(f"telemetry table {table} checksums do not match its chunks")
    return out


def manifest_chunks(manifest: dict, input_dir: Path) -> list[dict[str, Any]]:
    """Normalise both manifest formats for callers that perform their own validation."""
    version = str(manifest.get("schema_version") or manifest.get("telemetry_manifest_version") or "")
    out: list[dict[str, Any]] = []
    if version == "3.0":
        source = ((chunk.get("table"), chunk) for chunk in manifest.get("chunks", []))
    else:
        source = (
            (table, chunk) for table, meta in manifest.get("tables", {}).items() for chunk in meta.get("chunks", [])
        )
    for table, chunk in source:
        filename = chunk.get("file")
        if not filename:
            continue
        out.append(
            {
                "table": table,
                "chunk_id": chunk.get("chunk_id") or Path(filename).stem,
                "path": input_dir / filename,
                "sha256": chunk["sha256"],
                "row_count": int(chunk.get("row_count", 0)),
            }
        )
    return out


def _load_state(path: Path, migration_id: str, chunks: list[dict[str, Any]]) -> dict[str, dict]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise ArtifactValidationError(f"invalid telemetry import resume state: {path}") from exc
    if not isinstance(data, dict) or data.get("migration_id") != migration_id:
        raise ArtifactValidationError("telemetry import resume state does not match the manifest migration_id")
    completed = data.get("completed")
    if not isinstance(completed, dict):
        raise ArtifactValidationError("telemetry import resume state has invalid completed chunks")
    expected = {chunk["chunk_id"]: chunk for chunk in chunks}
    for chunk_id, state in completed.items():
        chunk = expected.get(chunk_id)
        if not chunk or not isinstance(state, dict):
            raise ArtifactValidationError(f"resume state contains unknown or invalid chunk: {chunk_id}")
        metadata = {"table": chunk["table"], "sha256": chunk["sha256"], "row_count": chunk["row_count"]}
        state_row_count = state.get("row_count")
        if (
            isinstance(state_row_count, bool)
            or not isinstance(state_row_count, int)
            or any(state.get(field) != value for field, value in metadata.items())
        ):
            raise ArtifactValidationError(f"resume state metadata changed for chunk {chunk_id}")
    return completed


def _write_state(path: Path, migration_id: str, completed: dict[str, dict]) -> None:
    write_manifest(path, {"migration_id": migration_id, "completed": completed})


async def _post_chunk(
    client: httpx.AsyncClient,
    params: TelemetryConnParams,
    *,
    migration_id: str,
    chunk: dict[str, Any],
    project_id: str | None,
) -> dict[str, Any]:
    form = {
        "migration_id": migration_id,
        "chunk_id": chunk["chunk_id"],
        "table": chunk["table"],
        "sha256": chunk["sha256"],
        "expected_row_count": str(chunk["row_count"]),
    }
    if project_id:
        form["project_id"] = project_id
    last_error: Exception | None = None
    for attempt in range(1, CHUNK_RETRY_ATTEMPTS + 1):
        try:
            with chunk["path"].open("rb") as fh:
                resp = await client.post(
                    f"{params.base_url}/v1/import/chunk",
                    data=form,
                    files={"file": (chunk["path"].name, fh, "application/octet-stream")},
                    headers=params.headers,
                    timeout=None,
                )
        except httpx.HTTPError as exc:
            last_error = exc
            optic.warning("chunk {} attempt {} transport failure: {}", chunk["chunk_id"], attempt, exc)
        else:
            if resp.status_code < 400:
                return resp.json()
            body = resp.text[:300]
            if resp.status_code in (400, 401, 413, 422):
                raise MigrationError(f"Chunk {chunk['chunk_id']} rejected ({resp.status_code}): {body}")
            last_error = MigrationError(f"Chunk {chunk['chunk_id']} failed ({resp.status_code}): {body}")
            optic.warning("chunk {} attempt {} failed: {}", chunk["chunk_id"], attempt, body)
        if attempt < CHUNK_RETRY_ATTEMPTS:
            await asyncio.sleep(min(CHUNK_RETRY_MAX_SECONDS, CHUNK_RETRY_BASE_SECONDS * (2 ** (attempt - 1))))
    raise MigrationError(f"Chunk {chunk['chunk_id']} failed after {CHUNK_RETRY_ATTEMPTS} attempts: {last_error}")


async def wait_for_job(
    client: httpx.AsyncClient,
    params: TelemetryConnParams,
    job_id: str,
    reporter: ProgressReporter,
    *,
    phase: str,
    pct_from: int,
    pct_to: int,
) -> dict[str, Any]:
    """Poll a store job until it finishes. No overall timeout; aborts only when progress stalls."""
    last_heartbeat = None
    stalled_since = time.monotonic()
    while True:
        resp = await client.get(f"{params.base_url}/v1/jobs/{job_id}", headers=params.headers, timeout=30.0)
        resp.raise_for_status()
        job = resp.json()
        if job.get("heartbeat_at") != last_heartbeat:
            last_heartbeat = job.get("heartbeat_at")
            stalled_since = time.monotonic()
        pct = pct_from + int((pct_to - pct_from) * (job.get("pct") or 0) / 100)
        await reporter.update(phase=phase, pct=pct, message=job.get("message") or phase)
        if job["state"] == "done":
            return job
        if job["state"] == "failed":
            raise MigrationError(f"telemetry job {job_id} failed: {job.get('error')}")
        if time.monotonic() - stalled_since > JOB_STALL_SECONDS:
            raise MigrationError(f"telemetry job {job_id} made no progress for {JOB_STALL_SECONDS // 60} minutes")
        await asyncio.sleep(JOB_POLL_SECONDS)


async def rebuild_derived(
    client: httpx.AsyncClient,
    params: TelemetryConnParams,
    reporter: ProgressReporter,
    *,
    migration_id: str,
    pct_from: int = 80,
    pct_to: int = 98,
) -> dict[str, Any]:
    resp = await client.post(
        f"{params.base_url}/v1/rebuild/derived",
        json={"all": True, "migration_id": migration_id},
        headers=params.headers,
        timeout=60.0,
    )
    if resp.status_code >= 400:
        raise MigrationError(f"rebuild request failed ({resp.status_code}): {resp.text[:300]}")
    job = await wait_for_job(
        client, params, resp.json()["id"], reporter, phase="rebuild", pct_from=pct_from, pct_to=pct_to
    )
    return job.get("result", {})


async def import_telemetry(
    params: TelemetryConnParams,
    input_dir: Path,
    reporter: ProgressReporter,
    *,
    project_id: str | None = None,
    rebuild: bool = True,
) -> TelemetryImportResult:
    """Import every chunk in *input_dir* into the telemetry store, then rebuild derived tables."""
    t0 = time.monotonic()
    manifest_path = input_dir / MANIFEST_FILENAME
    if not manifest_path.exists():
        raise MigrationError(f"Telemetry manifest not found: {manifest_path}")
    manifest = read_manifest(manifest_path)
    # Validate every entry and checksum before connecting to the destination. A
    # malformed later chunk must not leave an otherwise avoidable partial import.
    chunks = validate_manifest(manifest, input_dir, verify_artifacts=True)
    migration_id = manifest["migration_id"]
    skipped_tables = sorted({c["table"] for c in chunks if c["table"] in DERIVED_TABLES})
    chunks = [c for c in chunks if c["table"] in IMPORTED_TABLES]
    if skipped_tables:
        optic.info("derived tables are rebuilt, not imported: {}", skipped_tables)

    await connect_telemetry(params)
    await reporter.update(phase="telemetry_import", pct=0, message=f"Importing {len(chunks)} chunks")

    state_path = input_dir / IMPORT_STATE_FILENAME
    completed = _load_state(state_path, migration_id, chunks)
    imported: dict[str, int] = {}
    failed: list[str] = []
    rows_total = 0

    async with httpx.AsyncClient() as client:
        for idx, chunk in enumerate(chunks):
            pct = int(80 * idx / len(chunks)) if chunks else 80
            key = chunk["chunk_id"]
            if key in completed:
                rows_total += chunk["row_count"]
                imported[chunk["table"]] = imported.get(chunk["table"], 0) + chunk["row_count"]
                continue
            if not chunk["path"].is_file():
                failed.append(f"{chunk['chunk_id']} (missing file)")
                continue
            if _sha256(chunk["path"]) != chunk["sha256"]:
                failed.append(f"{chunk['chunk_id']} (checksum mismatch)")
                continue
            await reporter.update(
                phase="telemetry_import",
                pct=pct,
                message=f"Importing {chunk['table']} chunk {idx + 1}/{len(chunks)}",
            )
            result = await _post_chunk(client, params, migration_id=migration_id, chunk=chunk, project_id=project_id)
            row_count = int(result.get("row_count", chunk["row_count"]))
            rows_total += row_count
            imported[chunk["table"]] = imported.get(chunk["table"], 0) + row_count
            completed[key] = {
                "table": chunk["table"],
                "sha256": chunk["sha256"],
                "row_count": row_count,
                "skipped": bool(result.get("skipped")),
            }
            _write_state(state_path, migration_id, completed)

        if failed:
            raise MigrationError(f"{len(failed)} chunk(s) could not be imported: {failed[:10]}")

        rebuild_result: dict[str, Any] = {}
        if rebuild:
            rebuild_result = await rebuild_derived(client, params, reporter, migration_id=migration_id)

    await reporter.update(phase="telemetry_import", pct=100, message="Telemetry import complete")
    return TelemetryImportResult(
        migration_id=migration_id,
        tables_imported=imported,
        rows_imported=rows_total,
        failed_files=failed,
        duration_seconds=round(time.monotonic() - t0, 2),
        derived_rebuild=rebuild_result,
    )
