# SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""HTTP surface of the telemetry store."""

from __future__ import annotations

import asyncio
import hmac
import shutil
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import duckdb
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from loguru import logger as optic
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field

from telemetry_store import metrics
from telemetry_store import writer as w
from telemetry_store.db import Database
from telemetry_store.jobs import JobRegistry
from telemetry_store.reader import QueryRejectedError, QueryTimeoutError, Reader, ReaderBusyError, ResultTooLargeError
from telemetry_store.settings import TelemetrySettings

MAX_ROWS_PER_WRITE = 50_000


# ── Models ────────────────────────────────────────────────────────


class QueryRequest(BaseModel):
    sql: str = Field(..., min_length=1, max_length=200_000)
    params: dict[str, Any] = Field(default_factory=dict)
    timeout_ms: int | None = Field(None, ge=100, le=3_600_000)


class RowsRequest(BaseModel):
    table: str
    rows: list[dict[str, Any]] = Field(..., max_length=MAX_ROWS_PER_WRITE)


class SessionIdentity(BaseModel):
    project_id: str
    user_id: str
    harness: str
    session_id: str


class SessionBatchRequest(BaseModel):
    events: list[dict[str, Any]] = Field(default_factory=list, max_length=MAX_ROWS_PER_WRITE)
    refresh_summary: bool = True
    advance_checkpoint: SessionIdentity | None = None


class CheckpointRequest(SessionIdentity):
    acknowledged_line: int
    acknowledged_offset: int = 0


class DeleteRequest(BaseModel):
    table: str
    where: dict[str, Any]


class OrphanRequest(BaseModel):
    project_id: str


class ExpireRequest(BaseModel):
    before: str


class RebuildRequest(BaseModel):
    session_keys: list[int] | None = None
    all: bool = False
    migration_id: str | None = None


class ExportRequest(BaseModel):
    tables: list[str]
    #: Optional absolute path. When omitted the export lands under the store's own
    #: export directory and chunks are fetched through ``GET /v1/export/{job_id}/files/{name}``.
    dest_dir: str | None = None
    since: str | None = None


class BackupRequest(BaseModel):
    dest_path: str


class BackfillStateRequest(BaseModel):
    migration_id: str
    phase: str
    pct: int = Field(0, ge=0, le=100)
    message: str = ""


# ── App ───────────────────────────────────────────────────────────


class State:
    def __init__(self, settings: TelemetrySettings):
        self.settings = settings
        self.database = Database(settings)
        self.writer: w.Writer | None = None
        self.reader: Reader | None = None
        self.jobs: JobRegistry | None = None
        self._checkpoint_task: asyncio.Task | None = None

    def start(self) -> None:
        conn = self.database.open()
        self.database.apply_schema()
        self.writer = w.Writer(conn, pause_timeout_ms=self.settings.write_queue_timeout_ms)
        self.reader = Reader(
            conn,
            threads=self.settings.read_threads,
            queue_max=self.settings.read_queue_max,
            default_timeout_ms=self.settings.query_timeout_ms,
            max_rows=self.settings.max_result_rows,
        )
        self.jobs = JobRegistry(self.writer, conn)

    async def checkpoint_loop(self) -> None:
        interval = max(30, self.settings.checkpoint_interval_s)
        while True:
            await asyncio.sleep(interval)
            try:
                await self.writer.run_raw(lambda c: c.execute("CHECKPOINT"))
                metrics.checkpoints_total.inc()
            except Exception as exc:
                optic.warning("periodic checkpoint failed: {}", exc)

    async def stop(self) -> None:
        if self._checkpoint_task:
            self._checkpoint_task.cancel()
        if self.jobs:
            await self.jobs.shutdown()
        if self.reader:
            self.reader.shutdown()
        if self.writer:
            self.writer.shutdown()
        self.database.close()


def create_app(settings: TelemetrySettings | None = None) -> FastAPI:
    settings = settings or TelemetrySettings.from_env()
    settings.validate()
    state = State(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state.start()
        if not state.database.is_memory:
            state._checkpoint_task = asyncio.get_running_loop().create_task(state.checkpoint_loop())
        try:
            yield
        finally:
            await state.stop()

    app = FastAPI(title="Observal telemetry store", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.telemetry = state

    # ── Auth ──────────────────────────────────────────────────

    async def require_token(request: Request) -> None:
        if settings.allow_anonymous and not settings.token:
            return
        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(token.strip(), settings.token):
            raise HTTPException(status_code=401, detail={"code": "unauthorized", "message": "invalid token"})

    auth = Depends(require_token)

    # ── Error envelope ────────────────────────────────────────

    def _error(status: int, code: str, message: str) -> JSONResponse:
        return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})

    @app.exception_handler(HTTPException)
    async def _http_exc(request: Request, exc: HTTPException):
        detail = exc.detail
        if isinstance(detail, dict) and "code" in detail:
            return _error(exc.status_code, detail["code"], detail.get("message", ""))
        return _error(exc.status_code, "http_error", str(detail))

    @app.exception_handler(QueryRejectedError)
    async def _rejected(request: Request, exc: QueryRejectedError):
        return _error(400, "query_rejected", str(exc))

    @app.exception_handler(QueryTimeoutError)
    async def _timeout(request: Request, exc: QueryTimeoutError):
        metrics.query_timeouts_total.inc()
        return _error(504, "query_timeout", str(exc))

    @app.exception_handler(ResultTooLargeError)
    async def _too_large(request: Request, exc: ResultTooLargeError):
        return _error(413, "result_too_large", str(exc))

    @app.exception_handler(ReaderBusyError)
    async def _busy(request: Request, exc: ReaderBusyError):
        metrics.busy_total.inc()
        return JSONResponse(
            status_code=429,
            content={"error": {"code": "telemetry_busy", "message": str(exc)}},
            headers={"Retry-After": "1"},
        )

    @app.exception_handler(w.WriterPausedError)
    async def _paused(request: Request, exc: w.WriterPausedError):
        return JSONResponse(
            status_code=503,
            content={"error": {"code": "writer_paused", "message": str(exc)}},
            headers={"Retry-After": "5"},
        )

    @app.exception_handler(w.WriteError)
    async def _write_error(request: Request, exc: w.WriteError):
        return _error(422, "write_rejected", str(exc))

    @app.exception_handler(duckdb.Error)
    async def _duckdb_error(request: Request, exc: duckdb.Error):
        optic.error("duckdb error on {}: {}", request.url.path, exc)
        code = "query_error" if request.url.path.endswith("/query") else "write_failed"
        status = 400 if code == "query_error" else 500
        return _error(status, code, str(exc))

    # ── Health / stats ────────────────────────────────────────

    @app.get("/v1/health")
    async def health():
        try:
            state.database.open().cursor().execute("SELECT 1").fetchone()
        except Exception as exc:
            return _error(503, "unhealthy", str(exc))
        file_bytes, wal_bytes = state.database.file_sizes()
        return {
            "status": "ok",
            "schema_version": state.database.schema_version(),
            "db_path": str(settings.db_path),
            "file_bytes": file_bytes,
            "wal_bytes": wal_bytes,
            "memory_limit": settings.memory_limit,
            "writer_paused": state.writer.paused,
            "read_pending": state.reader.pending,
            "read_active": state.reader.active,
            "write_inflight": state.writer.inflight,
            "duckdb_version": duckdb.__version__,
        }

    @app.get("/v1/stats", dependencies=[auth])
    async def stats():
        counts = await asyncio.get_running_loop().run_in_executor(None, state.database.table_counts)
        file_bytes, wal_bytes = state.database.file_sizes()
        backfill = (
            state.database.open()
            .cursor()
            .execute(
                "SELECT migration_id, phase, pct, message, updated_at FROM telemetry_backfill_state "
                "ORDER BY updated_at DESC LIMIT 1"
            )
            .fetchone()
        )
        for name, count in counts.items():
            metrics.table_rows.labels(table=name).set(count)
        metrics.file_bytes.set(file_bytes)
        return {
            "tables": counts,
            "file_bytes": file_bytes,
            "wal_bytes": wal_bytes,
            "queries": state.reader.query_count,
            "query_timeouts": state.reader.timeout_count,
            "busy_rejections": state.reader.busy_count,
            "write_txns": state.writer.txn_count,
            "write_failures": state.writer.txn_failures,
            "backfill": (
                {
                    "migration_id": backfill[0],
                    "phase": backfill[1],
                    "pct": backfill[2],
                    "message": backfill[3],
                    "updated_at": str(backfill[4]),
                }
                if backfill
                else None
            ),
        }

    @app.get("/metrics")
    async def prom_metrics(request: Request):
        if not settings.metrics_public:
            await require_token(request)
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    # ── Query ─────────────────────────────────────────────────

    @app.post("/v1/query", dependencies=[auth])
    async def query(req: QueryRequest):
        with metrics.query_latency.time():
            return await state.reader.query(req.sql, req.params, req.timeout_ms)

    # ── Writes ────────────────────────────────────────────────

    async def _write(fn, *args, **kwargs):
        with metrics.write_latency.time():
            return await state.writer.run(fn, *args, **kwargs)

    @app.post("/v1/write/append", dependencies=[auth])
    async def write_append(req: RowsRequest):
        return await _write(w.append_rows, req.table, req.rows)

    @app.post("/v1/write/replace", dependencies=[auth])
    async def write_replace(req: RowsRequest):
        return await _write(w.replace_rows, req.table, req.rows)

    @app.post("/v1/write/session-batch", dependencies=[auth])
    async def write_session_batch(req: SessionBatchRequest):
        return await _write(
            w.session_batch,
            events=req.events,
            refresh_summary=req.refresh_summary,
            advance=req.advance_checkpoint.model_dump() if req.advance_checkpoint else None,
        )

    @app.post("/v1/write/checkpoint", dependencies=[auth])
    async def write_checkpoint(req: CheckpointRequest):
        return await _write(w.upsert_checkpoint, **req.model_dump())

    @app.post("/v1/write/delete", dependencies=[auth])
    async def write_delete(req: DeleteRequest):
        return await _write(w.delete_rows, req.table, req.where)

    @app.post("/v1/write/delete-orphan-summaries", dependencies=[auth])
    async def write_delete_orphans(req: OrphanRequest):
        return await _write(w.delete_orphan_summaries, req.project_id)

    @app.post("/v1/write/expire-raw-lines", dependencies=[auth])
    async def write_expire(req: ExpireRequest):
        return await _write(w.expire_raw_lines, req.before)

    @app.post("/v1/write/backfill-state", dependencies=[auth])
    async def write_backfill_state(req: BackfillStateRequest):
        await _write(w.set_backfill_state, req.migration_id, req.phase, req.pct, req.message)
        return {"ok": True}

    # ── Import ────────────────────────────────────────────────

    @app.post("/v1/import/chunk", dependencies=[auth])
    async def import_chunk(
        migration_id: str = Form(...),
        chunk_id: str = Form(...),
        table: str = Form(...),
        sha256: str = Form(...),
        project_id: str | None = Form(None),
        path: str | None = Form(None),
        file: UploadFile | None = File(None),
    ):
        tmp_dir: Path | None = None
        if file is not None:
            tmp_dir = Path(tempfile.mkdtemp(prefix="telemetry-chunk-", dir=str(settings.temp_dir)))
            parquet_path = tmp_dir / "chunk.parquet"
            with parquet_path.open("wb") as out:
                while block := await file.read(1 << 20):
                    out.write(block)
        elif path:
            parquet_path = Path(path)
            if not parquet_path.is_file():
                raise HTTPException(status_code=422, detail={"code": "missing_file", "message": path})
        else:
            raise HTTPException(status_code=422, detail={"code": "missing_chunk", "message": "file or path required"})
        try:
            with metrics.write_latency.time():
                return await state.writer.run(
                    w.import_chunk,
                    migration_id=migration_id,
                    chunk_id=chunk_id,
                    table_name=table,
                    sha256=sha256,
                    parquet_path=parquet_path,
                    project_id_override=project_id,
                )
        finally:
            if tmp_dir is not None:
                shutil.rmtree(tmp_dir, ignore_errors=True)

    # ── Jobs ──────────────────────────────────────────────────

    @app.post("/v1/rebuild/derived", dependencies=[auth], status_code=202)
    async def rebuild(req: RebuildRequest):
        if not req.all and not req.session_keys:
            raise HTTPException(status_code=422, detail={"code": "bad_request", "message": "session_keys or all"})
        job = state.jobs.start_rebuild(None if req.all else req.session_keys, req.migration_id)
        return job.to_dict()

    @app.post("/v1/export", dependencies=[auth], status_code=202)
    async def export(req: ExportRequest):
        dest = Path(req.dest_dir) if req.dest_dir else None
        try:
            job = state.jobs.start_export(req.tables, dest, req.since, export_root=settings.export_dir)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail={"code": "bad_request", "message": str(exc)}) from exc
        return job.to_dict()

    @app.get("/v1/export/{job_id}/files/{name:path}", dependencies=[auth])
    async def export_file(job_id: str, name: str):
        job = state.jobs.get(job_id)
        if job is None or job.kind != "export" or job.state != "done":
            raise HTTPException(status_code=404, detail={"code": "not_found", "message": job_id})
        root = Path(job.result["dest_dir"]).resolve()
        target = (root / name).resolve()
        if root not in target.parents or not target.is_file():
            raise HTTPException(status_code=404, detail={"code": "not_found", "message": name})
        return FileResponse(target, media_type="application/octet-stream", filename=target.name)

    @app.post("/v1/admin/backup", dependencies=[auth], status_code=202)
    async def backup(req: BackupRequest):
        return state.jobs.start_backup(Path(req.dest_path)).to_dict()

    @app.get("/v1/jobs/{job_id}", dependencies=[auth])
    async def job_status(job_id: str):
        job = state.jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail={"code": "not_found", "message": job_id})
        return job.to_dict()

    @app.get("/v1/jobs", dependencies=[auth])
    async def jobs_list():
        return [j.to_dict() for j in state.jobs.list()]

    # ── Admin ─────────────────────────────────────────────────

    @app.post("/v1/admin/checkpoint", dependencies=[auth])
    async def checkpoint():
        await state.writer.run_raw(lambda c: c.execute("CHECKPOINT"))
        metrics.checkpoints_total.inc()
        return {"ok": True}

    @app.post("/v1/admin/pause-writes", dependencies=[auth])
    async def pause_writes():
        state.writer.pause()
        return {"writer_paused": True}

    @app.post("/v1/admin/resume-writes", dependencies=[auth])
    async def resume_writes():
        state.writer.resume()
        return {"writer_paused": False}

    return app
