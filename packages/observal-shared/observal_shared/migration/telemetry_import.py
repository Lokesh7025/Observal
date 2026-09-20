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
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from loguru import logger as optic

from observal_shared.migration.archive import read_manifest
from observal_shared.migration.connections import TelemetryConnParams, connect_telemetry
from observal_shared.migration.exceptions import ArtifactValidationError, MigrationError
from observal_shared.migration.results import TelemetryImportResult
from observal_shared.telemetry_tables import DERIVED_TABLES, IMPORTED_TABLES

if TYPE_CHECKING:
    from observal_shared.migration.progress import ProgressReporter

MANIFEST_FILENAME = "telemetry_manifest.json"
IMPORT_STATE_FILENAME = "import_state.json"
CHUNK_RETRY_ATTEMPTS = 5
CHUNK_RETRY_BASE_SECONDS = 1.0
CHUNK_RETRY_MAX_SECONDS = 30.0
JOB_POLL_SECONDS = 2.0
JOB_STALL_SECONDS = 30 * 60


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def manifest_chunks(manifest: dict, input_dir: Path) -> list[dict[str, Any]]:
    """Normalise both manifest formats into ``{table, chunk_id, path, sha256, row_count}``."""
    version = str(manifest.get("schema_version") or manifest.get("telemetry_manifest_version") or "")
    out: list[dict[str, Any]] = []
    if version.startswith("3"):
        for chunk in manifest.get("chunks", []):
            out.append(
                {
                    "table": chunk["table"],
                    "chunk_id": chunk["chunk_id"],
                    "path": input_dir / chunk["file"],
                    "sha256": chunk["sha256"],
                    "row_count": int(chunk.get("row_count", 0)),
                }
            )
        return out
    # 2.0: tables -> chunks with file / sha256 / row_count / bucket / range.
    for table, meta in manifest.get("tables", {}).items():
        for chunk in meta.get("chunks", []):
            filename = chunk.get("file")
            if not filename:
                continue
            chunk_id = chunk.get("chunk_id") or Path(filename).stem
            out.append(
                {
                    "table": table,
                    "chunk_id": chunk_id,
                    "path": input_dir / filename,
                    "sha256": chunk["sha256"],
                    "row_count": int(chunk.get("row_count", 0)),
                }
            )
    return out


def _load_state(path: Path, migration_id: str) -> dict[str, dict]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}
    if data.get("migration_id") != migration_id:
        return {}
    return data.get("completed", {})


def _write_state(path: Path, migration_id: str, completed: dict[str, dict]) -> None:
    path.write_text(json.dumps({"migration_id": migration_id, "completed": completed}, indent=2))


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
    migration_id = manifest.get("migration_id")
    if not migration_id:
        raise ArtifactValidationError("telemetry manifest has no migration_id")

    chunks = manifest_chunks(manifest, input_dir)
    skipped_tables = sorted({c["table"] for c in chunks if c["table"] in DERIVED_TABLES})
    chunks = [c for c in chunks if c["table"] in IMPORTED_TABLES]
    if skipped_tables:
        optic.info("derived tables are rebuilt, not imported: {}", skipped_tables)

    await connect_telemetry(params)
    await reporter.update(phase="telemetry_import", pct=0, message=f"Importing {len(chunks)} chunks")

    state_path = input_dir / IMPORT_STATE_FILENAME
    completed = _load_state(state_path, migration_id)
    imported: dict[str, int] = {}
    failed: list[str] = []
    rows_total = 0

    async with httpx.AsyncClient() as client:
        for idx, chunk in enumerate(chunks):
            pct = int(80 * idx / len(chunks)) if chunks else 80
            key = f"{chunk['table']}:{chunk['chunk_id']}"
            if key in completed:
                rows_total += int(completed[key].get("row_count", 0))
                imported[chunk["table"]] = imported.get(chunk["table"], 0) + int(completed[key].get("row_count", 0))
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
            completed[key] = {"row_count": row_count, "skipped": bool(result.get("skipped"))}
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
