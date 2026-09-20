# SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Read path: bounded thread pool, read-only statement enforcement, timeouts."""

from __future__ import annotations

import asyncio
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import duckdb
from loguru import logger as optic

from telemetry_store.db import rows_to_dicts

_READ_ONLY_TYPES = {
    duckdb.StatementType.SELECT,
    duckdb.StatementType.EXPLAIN,
}


class QueryRejectedError(ValueError):
    """Statement is not a read-only query."""


class QueryTimeoutError(TimeoutError):
    """Query exceeded its budget and was interrupted."""


class ResultTooLargeError(ValueError):
    """Query produced more rows than the configured cap."""


class ReaderBusyError(RuntimeError):
    """Read queue is saturated."""


def assert_read_only(sql: str) -> None:
    try:
        statements = duckdb.extract_statements(sql)
    except duckdb.Error as exc:
        raise QueryRejectedError(f"could not parse statement: {exc}") from exc
    if len(statements) != 1:
        raise QueryRejectedError("exactly one statement is required")
    stmt = statements[0]
    if stmt.type not in _READ_ONLY_TYPES:
        raise QueryRejectedError(f"statement type {stmt.type.name} is not allowed on the query endpoint")
    # SELECT ... INTO / COPY are separate statement types in DuckDB, but
    # table-producing functions that touch the filesystem are not.
    lowered = sql.lower()
    for token in ("read_parquet", "read_csv", "read_json", "glob(", "attach ", "copy ", "install ", "load "):
        if token in lowered:
            raise QueryRejectedError(f"'{token.strip()}' is not allowed on the query endpoint")


_PARAM_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")


def prune_params(sql: str, params: dict[str, Any]) -> dict[str, Any]:
    """Drop parameters the statement never references; DuckDB rejects extras."""
    if not params:
        return {}
    used = set(_PARAM_RE.findall(sql))
    return {k: v for k, v in params.items() if k in used}


class Reader:
    def __init__(
        self,
        conn: duckdb.DuckDBPyConnection,
        *,
        threads: int,
        queue_max: int,
        default_timeout_ms: int,
        max_rows: int,
    ):
        self._conn = conn
        self._executor = ThreadPoolExecutor(max_workers=threads, thread_name_prefix="telemetry-reader")
        self._queue_max = queue_max
        self._default_timeout = default_timeout_ms
        self._max_rows = max_rows
        self._pending = 0
        self._active = 0  # worker threads currently executing (includes ones unwinding after interrupt)
        self._pending_lock = threading.Lock()
        self.query_count = 0
        self.timeout_count = 0
        self.busy_count = 0

    @property
    def pending(self) -> int:
        return self._pending

    @property
    def active(self) -> int:
        return self._active

    def _execute(self, sql: str, params: dict[str, Any], cursor_box: dict[str, Any]) -> tuple[list[str], list[tuple]]:
        with self._pending_lock:
            self._active += 1
        cursor = self._conn.cursor()
        cursor_box["cursor"] = cursor
        params = prune_params(sql, params)
        try:
            cursor.execute(sql, params or None)
            columns = [d[0] for d in cursor.description] if cursor.description else []
            rows = cursor.fetchmany(self._max_rows + 1)
            return columns, rows
        finally:
            cursor.close()
            with self._pending_lock:
                self._active -= 1

    async def query(self, sql: str, params: dict[str, Any] | None, timeout_ms: int | None) -> dict[str, Any]:
        assert_read_only(sql)
        with self._pending_lock:
            if self._pending >= self._queue_max:
                self.busy_count += 1
                raise ReaderBusyError(f"read queue saturated ({self._pending} pending)")
            self._pending += 1
        budget = (timeout_ms or self._default_timeout) / 1000
        cursor_box: dict[str, Any] = {}
        t0 = time.perf_counter()
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(self._executor, self._execute, sql, params or {}, cursor_box)
        try:
            columns, rows = await asyncio.wait_for(asyncio.shield(future), timeout=budget)
        except TimeoutError as exc:
            self.timeout_count += 1
            # An interrupt issued before the cursor has actually started executing is
            # lost, so keep re-issuing it until the worker unwinds (bounded wait).
            deadline = time.perf_counter() + 30
            while not future.done() and time.perf_counter() < deadline:
                cursor = cursor_box.get("cursor")
                if cursor is not None:
                    try:
                        cursor.interrupt()
                    except duckdb.Error:
                        pass
                try:
                    await asyncio.wait_for(asyncio.shield(future), timeout=0.1)
                except (TimeoutError, Exception):
                    pass
            if not future.done():
                optic.error("telemetry query did not stop after interrupt: {}", sql[:120])
            optic.warning("telemetry query interrupted after {:.0f}ms: {}", budget * 1000, sql[:120])
            raise QueryTimeoutError(f"query exceeded {int(budget * 1000)}ms") from exc
        finally:
            with self._pending_lock:
                self._pending -= 1
        self.query_count += 1
        truncated = len(rows) > self._max_rows
        if truncated:
            raise ResultTooLargeError(f"query produced more than {self._max_rows} rows")
        elapsed_ms = (time.perf_counter() - t0) * 1000
        return {
            "columns": columns,
            "rows": rows_to_dicts(columns, rows),
            "row_count": len(rows),
            "elapsed_ms": round(elapsed_ms, 2),
        }

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
