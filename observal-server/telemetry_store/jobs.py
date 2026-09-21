# SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Long-running jobs (export, backup, rebuild) with heartbeats and no HTTP timeout."""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import shutil
import uuid
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

from loguru import logger as optic

from observal_shared.telemetry_tables import TELEMETRY_TABLES
from telemetry_store import writer as w
from telemetry_store.sql import sql_string_literal

if TYPE_CHECKING:
    from pathlib import Path

    import duckdb

EXPORT_CHUNK_ROWS = 500_000
STALL_TIMEOUT_S = 30 * 60


@dataclass
class Job:
    id: str
    kind: str
    state: str = "queued"  # queued | running | done | failed
    pct: int = 0
    message: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    heartbeat_at: str | None = None
    error: str | None = None
    result: dict[str, Any] = field(default_factory=dict)
    # Server-owned artifact locations are deliberately excluded from API responses.
    artifact_dir: Path | None = field(default=None, repr=False)
    artifact_file: Path | None = field(default=None, repr=False)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("artifact_dir")
        data.pop("artifact_file")
        return data


def _now() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _copy(cursor: duckdb.DuckDBPyConnection, sql: str, params: dict[str, Any]) -> None:
    cursor.execute(sql, params or None)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _owned_child(root: Path, name: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    resolved_root = root.resolve()
    child = (resolved_root / name).resolve()
    if not child.is_relative_to(resolved_root):
        raise ValueError("artifact path escapes its configured root")
    return child


class JobRegistry:
    def __init__(self, writer: w.Writer, conn: duckdb.DuckDBPyConnection):
        self._writer = writer
        self._conn = conn
        self._jobs: dict[str, Job] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        return sorted(self._jobs.values(), key=lambda j: j.started_at or "", reverse=True)

    def _touch(self, job: Job, pct: int, message: str) -> None:
        job.pct = int(pct)
        job.message = message
        job.heartbeat_at = _now()

    def _start(self, kind: str, coro_factory) -> Job:
        job = Job(id=uuid.uuid4().hex, kind=kind, state="running", started_at=_now(), heartbeat_at=_now())
        self._jobs[job.id] = job

        async def runner():
            try:
                job.result = await coro_factory(job)
                job.state = "done"
                job.pct = 100
            except Exception as exc:  # job failures are surfaced, never swallowed
                job.state = "failed"
                job.error = f"{type(exc).__name__}: {exc}"
                optic.opt(exception=True).error("telemetry job {} ({}) failed", job.id, kind)
            finally:
                job.finished_at = _now()

        self._tasks[job.id] = asyncio.get_running_loop().create_task(runner())
        return job

    # ── Backup ────────────────────────────────────────────────────

    def start_backup(self, backup_root: Path) -> Job:
        artifact_id = uuid.uuid4().hex
        dest_path = _owned_child(backup_root, f"{artifact_id}.duckdb")

        async def run(job: Job) -> dict[str, Any]:
            try:
                self._touch(job, 5, "copying database snapshot")

                def body_named(conn: duckdb.DuckDBPyConnection) -> None:
                    name = str(conn.execute("SELECT current_database()").fetchone()[0]).replace('"', '""')
                    conn.execute(f"ATTACH {sql_string_literal(str(dest_path))} AS telemetry_backup")
                    try:
                        conn.execute(f'COPY FROM DATABASE "{name}" TO telemetry_backup')
                    finally:
                        conn.execute("DETACH telemetry_backup")

                await self._writer.run_raw(body_named)
                self._touch(job, 95, "verifying")
                size = dest_path.stat().st_size
                return {"file": "backup.duckdb", "bytes": size, "sha256": _sha256(dest_path)}
            except BaseException:
                dest_path.unlink(missing_ok=True)
                raise

        job = self._start("backup", run)
        job.artifact_file = dest_path
        return job

    # ── Export to Parquet ────────────────────────────────────────

    def start_export(self, tables: list[str], since: str | None, *, export_root: Path) -> Job:
        unknown = [t for t in tables if t not in TELEMETRY_TABLES]
        if unknown:
            raise ValueError(f"unknown tables: {unknown}")
        dest_dir = _owned_child(export_root, uuid.uuid4().hex)

        async def run(job: Job) -> dict[str, Any]:
            dest_dir.mkdir(mode=0o700)
            manifest: dict[str, Any] = {
                "telemetry_manifest_version": "3.0",
                "source": "duckdb",
                "exported_at": _now(),
                "since": since,
                "tables": {},
                "chunks": [],
            }
            total = len(tables)
            for idx, table in enumerate(tables):
                cfg = TELEMETRY_TABLES[table]
                where = ""
                params: dict[str, Any] = {}
                if since:
                    version = cfg.version_column or cfg.time_column
                    where = f' WHERE "{version}" > CAST($since AS TIMESTAMP)'
                    params["since"] = since
                cursor = self._conn.cursor()
                try:
                    count = int(cursor.execute(f'SELECT count(*) FROM "{table}"{where}', params).fetchone()[0])
                    manifest["tables"][table] = {"row_count": count}
                    # Empty tables produce no chunk files; the manifest still records row_count=0.
                    chunks = -(-count // EXPORT_CHUNK_ROWS)
                    order = ", ".join(f'"{k}"' for k in cfg.key_columns)
                    for c in range(chunks):
                        self._touch(
                            job, int(100 * (idx + c / chunks) / total), f"exporting {table} chunk {c + 1}/{chunks}"
                        )
                        path = dest_dir / table / f"{table}-{c:05d}.parquet"
                        path.parent.mkdir(parents=True, exist_ok=True)
                        sql = (
                            f'COPY (SELECT * FROM "{table}"{where} ORDER BY {order} '
                            f"LIMIT {EXPORT_CHUNK_ROWS} OFFSET {c * EXPORT_CHUNK_ROWS}) "
                            f"TO {sql_string_literal(str(path))} (FORMAT PARQUET, COMPRESSION ZSTD)"
                        )
                        await asyncio.get_running_loop().run_in_executor(None, _copy, cursor, sql, params)
                        rows = int(
                            cursor.execute(
                                f"SELECT count(*) FROM read_parquet({sql_string_literal(str(path))})"
                            ).fetchone()[0]
                        )
                        manifest["chunks"].append(
                            {
                                "table": table,
                                "chunk_id": f"{table}-{c:05d}",
                                "file": str(path.relative_to(dest_dir)),
                                "row_count": rows,
                                "size_bytes": path.stat().st_size,
                                "sha256": _sha256(path),
                            }
                        )
                finally:
                    cursor.close()
            (dest_dir / "telemetry_manifest.json").write_text(json.dumps(manifest, indent=2))
            return {
                "chunks": len(manifest["chunks"]),
                "files": [c["file"] for c in manifest["chunks"]] + ["telemetry_manifest.json"],
                "tables": manifest["tables"],
            }

        async def guarded_run(job: Job) -> dict[str, Any]:
            try:
                return await run(job)
            except BaseException:
                shutil.rmtree(dest_dir, ignore_errors=True)
                raise

        job = self._start("export", guarded_run)
        job.artifact_dir = dest_dir
        return job

    # ── Rebuild derived tables ────────────────────────────────────

    def start_rebuild(self, session_keys: list[int] | None, migration_id: str | None) -> Job:
        async def run(job: Job) -> dict[str, Any]:
            keys = session_keys
            if keys is None:
                keys = await self._writer.run_raw(w.list_session_keys)
            total = len(keys)
            done = 0
            summaries = 0
            checkpoints = 0
            for i in range(0, total, w.REBUILD_BATCH_SESSIONS):
                batch = keys[i : i + w.REBUILD_BATCH_SESSIONS]
                out = await self._writer.run(w.rebuild_derived_batch, batch)
                summaries += out["summaries"]
                checkpoints += out["checkpoints"]
                done += len(batch)
                pct = int(100 * done / total) if total else 100
                self._touch(job, pct, f"rebuilt {done}/{total} sessions")
                if migration_id:
                    await self._writer.run(w.set_backfill_state, migration_id, "rebuild", pct, job.message)
            if migration_id:
                await self._writer.run(w.set_backfill_state, migration_id, "done", 100, "backfill complete")
            return {"sessions": total, "summaries": summaries, "checkpoints": checkpoints}

        return self._start("rebuild", run)

    async def shutdown(self) -> None:
        for task in self._tasks.values():
            if not task.done():
                task.cancel()
