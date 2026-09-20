# SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Export a DuckDB telemetry store to checksummed Parquet chunks.

The export itself runs inside the store as a job (no HTTP timeout); this
module starts it, polls for progress, downloads every chunk, verifies the
SHA-256 from the manifest, and writes a ``3.0`` telemetry manifest that
:mod:`telemetry_import` and :mod:`validation` understand.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx
from loguru import logger as optic

from observal_shared.migration.connections import TelemetryConnParams, connect_telemetry
from observal_shared.migration.exceptions import ChecksumMismatchError, MigrationError
from observal_shared.migration.results import TelemetryExportResult
from observal_shared.migration.telemetry_import import MANIFEST_FILENAME, wait_for_job
from observal_shared.telemetry_tables import IMPORTED_TABLES

if TYPE_CHECKING:
    from pathlib import Path

    from observal_shared.migration.progress import ProgressReporter

TELEMETRY_MANIFEST_VERSION_V3 = "3.0"


async def _download(client: httpx.AsyncClient, url: str, headers: dict[str, str], dest: Path) -> str:
    dest.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    async with client.stream("GET", url, headers=headers, timeout=None) as resp:
        if resp.status_code >= 400:
            raise MigrationError(f"download failed ({resp.status_code}): {url}")
        with dest.open("wb") as fh:
            async for block in resp.aiter_bytes(1 << 20):
                fh.write(block)
                digest.update(block)
    return digest.hexdigest()


async def export_telemetry(
    params: TelemetryConnParams,
    output_dir: Path,
    reporter: ProgressReporter,
    *,
    migration_id: str,
    tables: tuple[str, ...] = IMPORTED_TABLES,
    since: str | None = None,
) -> TelemetryExportResult:
    """Export *tables* from the store into *output_dir* with a verified manifest."""
    t0 = time.monotonic()
    if output_dir.exists() and any(output_dir.iterdir()):
        raise MigrationError(f"Output directory is not empty: {output_dir}")
    dir_existed = output_dir.exists()
    os.makedirs(output_dir, mode=0o700, exist_ok=True)

    await connect_telemetry(params)
    try:
        async with httpx.AsyncClient() as client:
            await reporter.update(phase="telemetry_export", pct=0, message="Starting store export job")
            resp = await client.post(
                f"{params.base_url}/v1/export",
                json={"tables": list(tables), "since": since},
                headers=params.headers,
                timeout=60.0,
            )
            if resp.status_code >= 400:
                raise MigrationError(f"export request failed ({resp.status_code}): {resp.text[:300]}")
            job_id = resp.json()["id"]
            await wait_for_job(client, params, job_id, reporter, phase="telemetry_export", pct_from=0, pct_to=60)

            remote_manifest_path = output_dir / "remote_manifest.json"
            await _download(
                client,
                f"{params.base_url}/v1/export/{job_id}/files/{MANIFEST_FILENAME}",
                params.headers,
                remote_manifest_path,
            )
            remote = json.loads(remote_manifest_path.read_text())
            remote_manifest_path.unlink()

            chunks = remote.get("chunks", [])
            total_rows = 0
            total_size = 0
            for idx, chunk in enumerate(chunks):
                pct = 60 + int(38 * idx / max(1, len(chunks)))
                await reporter.update(phase="telemetry_export", pct=pct, message=f"Downloading {chunk['file']}")
                dest = output_dir / chunk["file"]
                digest = await _download(
                    client, f"{params.base_url}/v1/export/{job_id}/files/{chunk['file']}", params.headers, dest
                )
                if digest != chunk["sha256"]:
                    raise ChecksumMismatchError(f"checksum mismatch for {chunk['file']}")
                total_rows += int(chunk["row_count"])
                total_size += dest.stat().st_size

        manifest = {
            "schema_version": TELEMETRY_MANIFEST_VERSION_V3,
            "telemetry_manifest_version": TELEMETRY_MANIFEST_VERSION_V3,
            "source": "duckdb",
            "migration_id": migration_id,
            "phase": "telemetry",
            "phase_status": "export_complete",
            "export_completed_at": datetime.now(UTC).isoformat(),
            "export_time_cutoff": remote.get("exported_at"),
            "since": since,
            "tables": remote.get("tables", {}),
            "chunks": chunks,
        }
        (output_dir / MANIFEST_FILENAME).write_text(json.dumps(manifest, indent=2))
        optic.info("telemetry export complete: {} rows in {} chunk(s)", total_rows, len(chunks))
        await reporter.update(phase="telemetry_export", pct=100, message="Telemetry export complete")
        return TelemetryExportResult(
            output_dir=str(output_dir),
            migration_id=migration_id,
            table_results={t: {"row_count": m.get("row_count", 0)} for t, m in remote.get("tables", {}).items()},
            total_rows=total_rows,
            total_size_bytes=total_size,
            duration_seconds=round(time.monotonic() - t0, 2),
        )
    except Exception:
        if not dir_existed and output_dir.exists():
            shutil.rmtree(output_dir, ignore_errors=True)
        raise
