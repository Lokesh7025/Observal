# SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Single-writer transaction primitives.

Every public function here runs inside one DuckDB transaction on the writer
thread. Replace semantics are always ``DELETE keys; INSERT rows`` - never
``INSERT OR REPLACE`` and never a constraint.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import time
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any, TypeVar

import duckdb
import pyarrow as pa
from loguru import logger as optic

from observal_shared.telemetry_keys import parent_session_key, session_key, snapshot_key
from observal_shared.telemetry_tables import (
    DERIVED_TABLES,
    IMPORTED_TABLES,
    PROJECT_SCOPED_TABLES,
    TELEMETRY_TABLES,
    TelemetryTable,
)
from observal_shared.telemetry_tables import get_table as _get_table
from telemetry_store.sql import (
    CONTIGUOUS_CHECKPOINT,
    CONTIGUOUS_CHECKPOINT_BATCH,
    SUMMARY_COLUMNS,
    SUMMARY_SELECT,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from pathlib import Path

T = TypeVar("T")

REBUILD_BATCH_SESSIONS = 500


class WriterPausedError(RuntimeError):
    """Writes are paused and the caller's wait budget expired."""


class WriteError(ValueError):
    """Caller supplied an invalid write request."""


def get_table(name: str) -> TelemetryTable:
    try:
        return _get_table(name)
    except KeyError as exc:
        raise WriteError(str(exc.args[0])) from exc


# ── Row preparation ────────────────────────────────────────────────


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


def _now_version() -> int:
    return time.time_ns()


def attach_keys(table: TelemetryTable, rows: list[dict[str, Any]]) -> None:
    """Compute derived key columns in place."""
    if table.name in {"session_events", "session_checkpoints", "session_stats_agg"}:
        for row in rows:
            row["session_key"] = session_key(
                str(row.get("project_id", "")),
                str(row.get("user_id", "")),
                str(row.get("harness", "")),
                str(row.get("session_id", "")),
            )
            if table.name == "session_events":
                row["parent_session_key"] = parent_session_key(
                    str(row.get("project_id", "")),
                    str(row.get("user_id", "")),
                    str(row.get("harness", "")),
                    row.get("parent_session_id"),
                )
    elif table.name == "layer_snapshots":
        for row in rows:
            row["snapshot_key"] = snapshot_key(
                str(row.get("project_id", "")), str(row.get("user_id", "")), str(row.get("hash", ""))
            )


def _column_types(conn: duckdb.DuckDBPyConnection, table: str) -> dict[str, str]:
    rows = conn.execute(
        "SELECT column_name, data_type FROM information_schema.columns WHERE table_name = $t ORDER BY ordinal_position",
        {"t": table},
    ).fetchall()
    return {name: data_type for name, data_type in rows}


def _arrow_from_rows(columns: Iterable[str], rows: list[dict[str, Any]]) -> pa.Table:
    """Build an Arrow table with one column per target column (all as strings or native)."""
    cols = list(columns)
    data: dict[str, list[Any]] = {c: [] for c in cols}
    for row in rows:
        for c in cols:
            value = row.get(c)
            if isinstance(value, dt.datetime):
                value = value.astimezone(dt.UTC).replace(tzinfo=None).isoformat(sep=" ", timespec="milliseconds")
            elif isinstance(value, (dict, list)):
                raise WriteError(f"column {c} must be scalar, got {type(value).__name__}")
            data[c].append(value)
    arrays = []
    for c in cols:
        values = data[c]
        try:
            arr = pa.array(values)
        except (pa.ArrowInvalid, pa.ArrowTypeError):
            arr = pa.array([None if v is None else str(v) for v in values], type=pa.string())
        if pa.types.is_null(arr.type):
            arr = arr.cast(pa.string())
        arrays.append(arr)
    return pa.table(arrays, names=cols)


def _cast_select(columns: Iterable[str], types: dict[str, str], source: str) -> str:
    parts = []
    for c in columns:
        target = types[c]
        if target == "BOOLEAN":
            # Accept 0/1, 'true'/'false', and booleans.
            parts.append(
                f"CASE WHEN try_cast(\"{c}\" AS VARCHAR) IN ('1', 'true', 'True') THEN true "
                f"WHEN try_cast(\"{c}\" AS VARCHAR) IN ('0', 'false', 'False') THEN false "
                f'ELSE CAST("{c}" AS BOOLEAN) END AS "{c}"'
            )
        else:
            parts.append(f'CAST("{c}" AS {target}) AS "{c}"')
    return f"SELECT {', '.join(parts)} FROM {source}"


def _stage_rows(conn: duckdb.DuckDBPyConnection, table: TelemetryTable, rows: list[dict[str, Any]]) -> str:
    """Register incoming rows as a typed temporary view. Returns the view name."""
    types = _column_types(conn, table.name)
    unknown = {k for row in rows for k in row} - set(types)
    if unknown:
        raise WriteError(f"unknown columns for {table.name}: {sorted(unknown)}")
    present = [c for c in table.columns if c in types and any(c in row for row in rows)]
    if not present:
        raise WriteError("rows contain no known columns")
    raw_view = f"_incoming_raw_{table.name}"
    typed_view = f"_incoming_{table.name}"
    conn.register(raw_view, _arrow_from_rows(present, rows))
    conn.execute(f"CREATE OR REPLACE TEMP VIEW {typed_view} AS {_cast_select(present, types, raw_view)}")
    return typed_view


def _insert_from_view(conn: duckdb.DuckDBPyConnection, table: str, view: str, columns: list[str]) -> int:
    cols = ", ".join(f'"{c}"' for c in columns)
    conn.execute(f'INSERT INTO "{table}" ({cols}) SELECT {cols} FROM {view}')
    return int(conn.execute(f"SELECT count(*) FROM {view}").fetchone()[0])


def _view_columns(conn: duckdb.DuckDBPyConnection, view: str) -> list[str]:
    return [d[0] for d in conn.execute(f"SELECT * FROM {view} LIMIT 0").description]


def _delete_matching_keys(conn: duckdb.DuckDBPyConnection, table: TelemetryTable, view: str) -> int:
    keys = ", ".join(f'"{k}"' for k in table.key_columns)
    before = conn.execute(f'SELECT count(*) FROM "{table.name}"').fetchone()[0]
    conn.execute(f'DELETE FROM "{table.name}" WHERE ({keys}) IN (SELECT {keys} FROM {view})')
    after = conn.execute(f'SELECT count(*) FROM "{table.name}"').fetchone()[0]
    return int(before - after)


# ── Transaction bodies (sync, run on the writer thread) ─────────────


def append_rows(conn: duckdb.DuckDBPyConnection, table_name: str, rows: list[dict[str, Any]]) -> dict[str, int]:
    table = get_table(table_name)
    if table.mode != "append":
        raise WriteError(f"{table_name} is not an append table")
    if not rows:
        return {"rows_written": 0}
    view = _stage_rows(conn, table, rows)
    written = _insert_from_view(conn, table.name, view, _view_columns(conn, view))
    return {"rows_written": written}


def replace_rows(conn: duckdb.DuckDBPyConnection, table_name: str, rows: list[dict[str, Any]]) -> dict[str, int]:
    table = get_table(table_name)
    if table.mode != "replace":
        raise WriteError(f"{table_name} is not a replace table")
    if not rows:
        return {"rows_written": 0, "rows_replaced": 0}
    attach_keys(table, rows)
    view = _stage_rows(conn, table, rows)
    replaced = _delete_matching_keys(conn, table, view)
    written = _insert_from_view(conn, table.name, view, _view_columns(conn, view))
    return {"rows_written": written, "rows_replaced": replaced}


def refresh_summaries(conn: duckdb.DuckDBPyConnection, keys: list[int]) -> int:
    if not keys:
        return 0
    conn.execute("DELETE FROM session_stats_agg WHERE session_key IN (SELECT unnest($keys::BIGINT[]))", {"keys": keys})
    conn.execute(
        f"INSERT INTO session_stats_agg ({SUMMARY_COLUMNS}) {SUMMARY_SELECT}",
        {"keys": keys, "version": _now_version()},
    )
    return len(keys)


def read_checkpoint(conn: duckdb.DuckDBPyConnection, key: int) -> tuple[int, int]:
    row = conn.execute(
        "SELECT acknowledged_line, acknowledged_offset FROM session_checkpoints WHERE session_key = $k "
        "ORDER BY checkpoint_version DESC LIMIT 1",
        {"k": key},
    ).fetchone()
    if not row:
        return -1, 0
    return int(row[0]), int(row[1] or 0)


def upsert_checkpoint(
    conn: duckdb.DuckDBPyConnection,
    *,
    project_id: str,
    user_id: str,
    harness: str,
    session_id: str,
    acknowledged_line: int,
    acknowledged_offset: int,
) -> dict[str, int]:
    key = session_key(project_id, user_id, harness, session_id)
    conn.execute("DELETE FROM session_checkpoints WHERE session_key = $k", {"k": key})
    conn.execute(
        "INSERT INTO session_checkpoints (session_key, project_id, user_id, harness, session_id, acknowledged_line, "
        "acknowledged_offset, checkpoint_version) VALUES ($k, $p, $u, $h, $s, $line, $off, $v)",
        {
            "k": key,
            "p": project_id,
            "u": user_id,
            "h": harness,
            "s": session_id,
            "line": int(acknowledged_line),
            "off": int(acknowledged_offset),
            "v": _now_version(),
        },
    )
    return {"acknowledged_line": int(acknowledged_line), "acknowledged_offset": int(acknowledged_offset)}


def advance_checkpoint(
    conn: duckdb.DuckDBPyConnection,
    *,
    project_id: str,
    user_id: str,
    harness: str,
    session_id: str,
) -> dict[str, int]:
    """Advance the durable contiguous checkpoint from canonical source rows."""
    key = session_key(project_id, user_id, harness, session_id)
    ack_line, ack_offset = read_checkpoint(conn, key)
    row = conn.execute(CONTIGUOUS_CHECKPOINT, {"key": key, "ack": ack_line}).fetchone()
    if row and row[0] is not None:
        ack_line, ack_offset = int(row[0]), int(row[1] or 0)
    return upsert_checkpoint(
        conn,
        project_id=project_id,
        user_id=user_id,
        harness=harness,
        session_id=session_id,
        acknowledged_line=ack_line,
        acknowledged_offset=ack_offset,
    )


def session_batch(
    conn: duckdb.DuckDBPyConnection,
    *,
    events: list[dict[str, Any]],
    refresh_summary: bool,
    advance: dict[str, str] | None,
) -> dict[str, Any]:
    """Ingest hot path: replace events, refresh summaries, advance checkpoint - one transaction."""
    result: dict[str, Any] = {"events_written": 0, "events_replaced": 0, "summaries_refreshed": 0}
    keys: set[int] = set()
    if events:
        written = replace_rows(conn, "session_events", events)
        result["events_written"] = written["rows_written"]
        result["events_replaced"] = written["rows_replaced"]
        keys.update(int(e["session_key"]) for e in events)
    if advance:
        keys.add(session_key(advance["project_id"], advance["user_id"], advance["harness"], advance["session_id"]))
    if refresh_summary and keys:
        result["summaries_refreshed"] = refresh_summaries(conn, sorted(keys))
    if advance:
        result["checkpoint"] = advance_checkpoint(conn, **advance)
    return result


_DELETE_PREDICATES = {"timestamp_lt", "project_id", "session_keys", "older_than_days"}


def delete_rows(conn: duckdb.DuckDBPyConnection, table_name: str, where: dict[str, Any]) -> dict[str, int]:
    """Bounded-predicate delete. Never accepts raw SQL."""
    table = get_table(table_name)
    unknown = set(where) - _DELETE_PREDICATES
    if unknown:
        raise WriteError(f"unsupported delete predicates: {sorted(unknown)}")
    if not where:
        raise WriteError("delete requires at least one predicate")
    clauses: list[str] = []
    params: dict[str, Any] = {}
    if "timestamp_lt" in where:
        clauses.append(f'"{table.time_column}" < CAST($ts AS TIMESTAMP)')
        params["ts"] = str(where["timestamp_lt"])
    if "older_than_days" in where:
        clauses.append(f'"{table.time_column}" < current_timestamp::TIMESTAMP - to_days(CAST($days AS INTEGER))')
        params["days"] = int(where["older_than_days"])
    if "project_id" in where:
        if table.name not in PROJECT_SCOPED_TABLES:
            raise WriteError(f"{table.name} is not project scoped")
        clauses.append("project_id = $pid")
        params["pid"] = str(where["project_id"])
    if "session_keys" in where:
        if "session_key" not in table.columns:
            raise WriteError(f"{table.name} has no session_key")
        clauses.append("session_key IN (SELECT unnest($keys::BIGINT[]))")
        params["keys"] = [int(k) for k in where["session_keys"]]
    before = conn.execute(f'SELECT count(*) FROM "{table.name}"').fetchone()[0]
    conn.execute(f'DELETE FROM "{table.name}" WHERE {" AND ".join(clauses)}', params)
    after = conn.execute(f'SELECT count(*) FROM "{table.name}"').fetchone()[0]
    return {"rows_deleted": int(before - after)}


def delete_orphan_summaries(conn: duckdb.DuckDBPyConnection, project_id: str) -> dict[str, int]:
    deleted = 0
    for table in DERIVED_TABLES:
        before = conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
        conn.execute(
            f'DELETE FROM "{table}" WHERE project_id = $pid AND session_key NOT IN '
            "(SELECT DISTINCT session_key FROM session_events WHERE project_id = $pid)",
            {"pid": project_id},
        )
        after = conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
        deleted += int(before - after)
    return {"rows_deleted": deleted}


def expire_raw_lines(conn: duckdb.DuckDBPyConnection, before: str) -> dict[str, int]:
    """Drop ``raw_line`` bodies older than ``before`` (ClickHouse column TTL equivalent)."""
    row = conn.execute(
        "SELECT count(*) FROM session_events WHERE \"timestamp\" < CAST($ts AS TIMESTAMP) AND raw_line <> ''",
        {"ts": before},
    ).fetchone()
    affected = int(row[0]) if row else 0
    if affected:
        conn.execute(
            "UPDATE session_events SET raw_line = '', raw_line_truncated = 2 "
            "WHERE \"timestamp\" < CAST($ts AS TIMESTAMP) AND raw_line <> ''",
            {"ts": before},
        )
    return {"rows_expired": affected}


def import_chunk(
    conn: duckdb.DuckDBPyConnection,
    *,
    migration_id: str,
    chunk_id: str,
    table_name: str,
    sha256: str,
    parquet_path: Path,
    project_id_override: str | None = None,
) -> dict[str, Any]:
    """Idempotently import one checksummed Parquet chunk exported from ClickHouse."""
    if table_name not in IMPORTED_TABLES:
        raise WriteError(f"{table_name} is not importable (derived tables are rebuilt)")
    table = get_table(table_name)
    ledger = conn.execute(
        "SELECT row_count FROM telemetry_import_ledger WHERE migration_id = $m AND chunk_id = $c",
        {"m": migration_id, "c": chunk_id},
    ).fetchone()
    if ledger:
        return {"skipped": True, "rows_written": 0, "row_count": int(ledger[0])}

    digest = hashlib.sha256()
    with parquet_path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    if digest.hexdigest() != sha256:
        raise WriteError(f"checksum mismatch for chunk {chunk_id}")

    types = _column_types(conn, table.name)
    src = f"read_parquet('{parquet_path.as_posix()}')"
    src_cols = [d[0] for d in conn.execute(f"SELECT * FROM {src} LIMIT 0").description]
    known = [c for c in table.columns if c in src_cols and c in types]
    if not known:
        raise WriteError(f"chunk {chunk_id} shares no columns with {table.name}")

    select_parts = []
    for c in known:
        if c == "project_id" and project_id_override:
            select_parts.append(f'$pid AS "{c}"')
        elif types[c] == "BOOLEAN":
            select_parts.append(f'(try_cast("{c}" AS INTEGER) <> 0 OR try_cast("{c}" AS BOOLEAN)) AS "{c}"')
        else:
            select_parts.append(f'CAST("{c}" AS {types[c]}) AS "{c}"')
    columns = list(known)
    if table.name == "session_events":
        columns += ["session_key", "parent_session_key"]
    elif table.name == "layer_snapshots":
        columns.append("snapshot_key")

    conn.execute(
        f"CREATE OR REPLACE TEMP TABLE _chunk_src AS SELECT {', '.join(select_parts)} FROM {src}",
        {"pid": project_id_override} if project_id_override else {},
    )
    row_count = int(conn.execute("SELECT count(*) FROM _chunk_src").fetchone()[0])

    # Derived keys: computed in Python so there is one hash implementation everywhere.
    if table.name in {"session_events", "layer_snapshots"}:
        if table.name == "session_events":
            ident = conn.execute(
                "SELECT DISTINCT project_id, user_id, harness, session_id, parent_session_id FROM _chunk_src"
            ).fetchall()
            key_rows = [
                {
                    "project_id": p,
                    "user_id": u,
                    "harness": h,
                    "session_id": s,
                    "parent_session_id": ps,
                    "session_key": session_key(p or "", u or "", h or "", s or ""),
                    "parent_session_key": parent_session_key(p or "", u or "", h or "", ps),
                }
                for p, u, h, s, ps in ident
            ]
            conn.register("_chunk_keys", _arrow_from_rows(list(key_rows[0].keys()) if key_rows else [], key_rows))
            conn.execute(
                "CREATE OR REPLACE TEMP TABLE _chunk_typed AS "
                "SELECT s.*, k.session_key::BIGINT AS session_key, k.parent_session_key::BIGINT AS parent_session_key "
                "FROM _chunk_src s JOIN _chunk_keys k USING (project_id, user_id, harness, session_id) "
                "WHERE s.parent_session_id IS NOT DISTINCT FROM k.parent_session_id"
            )
        else:
            ident = conn.execute("SELECT DISTINCT project_id, user_id, hash FROM _chunk_src").fetchall()
            key_rows = [
                {"project_id": p, "user_id": u, "hash": h, "snapshot_key": snapshot_key(p or "", u or "", h or "")}
                for p, u, h in ident
            ]
            conn.register("_chunk_keys", _arrow_from_rows(list(key_rows[0].keys()) if key_rows else [], key_rows))
            conn.execute(
                "CREATE OR REPLACE TEMP TABLE _chunk_typed AS "
                "SELECT s.*, k.snapshot_key::BIGINT AS snapshot_key "
                "FROM _chunk_src s JOIN _chunk_keys k USING (project_id, user_id, hash)"
            )
    else:
        conn.execute("CREATE OR REPLACE TEMP TABLE _chunk_typed AS SELECT * FROM _chunk_src")

    replaced = 0
    if table.mode == "replace":
        # Never overwrite rows written live after the export cutoff: only rows whose
        # version is not newer than the chunk's version for the same key are replaced.
        keys = ", ".join(f'"{k}"' for k in table.key_columns)
        version = table.version_column or table.time_column
        before = conn.execute(f'SELECT count(*) FROM "{table.name}"').fetchone()[0]
        conn.execute(
            f'DELETE FROM "{table.name}" t WHERE EXISTS (SELECT 1 FROM _chunk_typed c '
            f"WHERE ({', '.join(f't.{k}' for k in table.key_columns)}) = ({', '.join(f'c.{k}' for k in table.key_columns)}) "
            f'AND t."{version}" <= c."{version}")'
        )
        after = conn.execute(f'SELECT count(*) FROM "{table.name}"').fetchone()[0]
        replaced = int(before - after)
        cols = ", ".join(f'"{c}"' for c in columns)
        conn.execute(
            f'INSERT INTO "{table.name}" ({cols}) SELECT {cols} FROM _chunk_typed c '
            f'WHERE NOT EXISTS (SELECT 1 FROM "{table.name}" t WHERE ({keys}) = '
            f"({', '.join(f'c.{k}' for k in table.key_columns)}))"
        )
    else:
        key = table.key_columns[0]
        cols = ", ".join(f'"{c}"' for c in columns)
        conn.execute(
            f'INSERT INTO "{table.name}" ({cols}) SELECT {cols} FROM _chunk_typed c '
            f'WHERE NOT EXISTS (SELECT 1 FROM "{table.name}" t WHERE t."{key}" = c."{key}")'
        )
    written = int(conn.execute("SELECT count(*) FROM _chunk_typed").fetchone()[0])
    conn.execute(
        'INSERT INTO telemetry_import_ledger (migration_id, chunk_id, "table", sha256, row_count) '
        "VALUES ($m, $c, $t, $s, $n)",
        {"m": migration_id, "c": chunk_id, "t": table.name, "s": sha256, "n": row_count},
    )
    conn.execute("DROP TABLE IF EXISTS _chunk_src")
    conn.execute("DROP TABLE IF EXISTS _chunk_typed")
    return {"skipped": False, "rows_written": written, "rows_replaced": replaced, "row_count": row_count}


def rebuild_derived_batch(conn: duckdb.DuckDBPyConnection, keys: list[int]) -> dict[str, int]:
    """Rebuild summaries and checkpoints for a batch of sessions.

    Checkpoints never regress: the rebuilt contiguous line is combined with any
    live checkpoint using ``max``.
    """
    if not keys:
        return {"summaries": 0, "checkpoints": 0}
    refresh_summaries(conn, keys)
    rows = conn.execute(CONTIGUOUS_CHECKPOINT_BATCH, {"keys": keys}).fetchall()
    existing = {
        int(r[0]): (int(r[1]), int(r[2] or 0))
        for r in conn.execute(
            "SELECT session_key, acknowledged_line, acknowledged_offset FROM session_checkpoints "
            "WHERE session_key IN (SELECT unnest($keys::BIGINT[]))",
            {"keys": keys},
        ).fetchall()
    }
    version = _now_version()
    out: list[dict[str, Any]] = []
    for key, project_id, user_id, harness, session_id, line, offset in rows:
        line = int(line)
        offset = int(offset or 0)
        if key in existing and existing[key][0] > line:
            line, offset = existing[key]
        out.append(
            {
                "session_key": int(key),
                "project_id": project_id,
                "user_id": user_id,
                "harness": harness,
                "session_id": session_id,
                "acknowledged_line": line,
                "acknowledged_offset": offset,
                "checkpoint_version": version,
            }
        )
    if out:
        conn.execute(
            "DELETE FROM session_checkpoints WHERE session_key IN (SELECT unnest($keys::BIGINT[]))",
            {"keys": [o["session_key"] for o in out]},
        )
        conn.register("_rebuild_ckpt", _arrow_from_rows(list(out[0].keys()), out))
        conn.execute(
            "INSERT INTO session_checkpoints (session_key, project_id, user_id, harness, session_id, "
            "acknowledged_line, acknowledged_offset, checkpoint_version) "
            "SELECT session_key::BIGINT, project_id, user_id, harness, session_id, acknowledged_line::BIGINT, "
            "acknowledged_offset::UBIGINT, checkpoint_version::UBIGINT FROM _rebuild_ckpt"
        )
        conn.unregister("_rebuild_ckpt")
    return {"summaries": len(keys), "checkpoints": len(out)}


def list_session_keys(conn: duckdb.DuckDBPyConnection) -> list[int]:
    return [int(r[0]) for r in conn.execute("SELECT DISTINCT session_key FROM session_events ORDER BY 1").fetchall()]


def set_backfill_state(conn: duckdb.DuckDBPyConnection, migration_id: str, phase: str, pct: int, message: str) -> None:
    conn.execute("DELETE FROM telemetry_backfill_state WHERE migration_id = $m", {"m": migration_id})
    conn.execute(
        "INSERT INTO telemetry_backfill_state (migration_id, phase, pct, message) VALUES ($m, $p, $pct, $msg)",
        {"m": migration_id, "p": phase, "pct": int(pct), "msg": message},
    )


# ── Writer: serialises transactions on one thread ─────────────────


class Writer:
    """Runs transaction bodies one at a time on a dedicated thread."""

    def __init__(self, conn: duckdb.DuckDBPyConnection, *, pause_timeout_ms: int):
        self._conn = conn
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="telemetry-writer")
        self._resumed = asyncio.Event()
        self._resumed.set()
        self._pause_timeout = pause_timeout_ms / 1000
        self.txn_count = 0
        self.txn_failures = 0
        self.inflight = 0

    @property
    def paused(self) -> bool:
        return not self._resumed.is_set()

    def pause(self) -> None:
        self._resumed.clear()
        optic.warning("telemetry writes paused")

    def resume(self) -> None:
        self._resumed.set()
        optic.info("telemetry writes resumed")

    def _run_txn(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        conn = self._conn
        conn.execute("BEGIN TRANSACTION")
        try:
            result = fn(conn, *args, **kwargs)
            conn.execute("COMMIT")
            self.txn_count += 1
            return result
        except Exception:
            self.txn_failures += 1
            try:
                conn.execute("ROLLBACK")
            except duckdb.Error:
                pass
            raise

    async def run(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        if self.paused:
            try:
                await asyncio.wait_for(self._resumed.wait(), timeout=self._pause_timeout)
            except TimeoutError as exc:
                raise WriterPausedError("telemetry writer is paused") from exc
        loop = asyncio.get_running_loop()
        self.inflight += 1
        try:
            return await loop.run_in_executor(self._executor, lambda: self._run_txn(fn, *args, **kwargs))
        finally:
            self.inflight -= 1

    async def run_raw(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """Run a non-transactional statement (CHECKPOINT, ATTACH/COPY) on the writer thread."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, lambda: fn(self._conn, *args, **kwargs))

    def shutdown(self) -> None:
        self._executor.shutdown(wait=True)


__all__ = [
    "REBUILD_BATCH_SESSIONS",
    "TELEMETRY_TABLES",
    "WriteError",
    "Writer",
    "WriterPausedError",
    "advance_checkpoint",
    "append_rows",
    "attach_keys",
    "delete_orphan_summaries",
    "delete_rows",
    "expire_raw_lines",
    "import_chunk",
    "list_session_keys",
    "read_checkpoint",
    "rebuild_derived_batch",
    "refresh_summaries",
    "replace_rows",
    "session_batch",
    "set_backfill_state",
    "upsert_checkpoint",
]
