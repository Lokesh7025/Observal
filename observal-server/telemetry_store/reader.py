# SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Read path: structural query sandbox, bounded pool, and timeouts."""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import duckdb
from loguru import logger as optic

from observal_shared.telemetry_tables import TELEMETRY_TABLES
from telemetry_store.db import rows_to_dicts

_ALLOWED_QUERY_TABLES = set(TELEMETRY_TABLES) | {
    "schema_migrations",
    "telemetry_backfill_state",
    "telemetry_import_ledger",
}
_ALLOWED_SCHEMAS = {"", "main"}
_BLOCKED_SCALAR_FUNCTIONS = {
    "current_query",
    "current_setting",
    "getenv",
}


class QueryRejectedError(ValueError):
    """Statement is not a safe telemetry read query."""


class QueryTimeoutError(TimeoutError):
    """Query exceeded its budget and was interrupted."""


class ResultTooLargeError(ValueError):
    """Query produced more rows than the configured cap."""


class ReaderBusyError(RuntimeError):
    """Read queue is saturated."""


def _walk_ast(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_ast(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_ast(child)


def _parse_select(parser: duckdb.DuckDBPyConnection, sql: str) -> dict[str, Any]:
    try:
        statements = duckdb.extract_statements(sql)
    except duckdb.Error as exc:
        raise QueryRejectedError("could not parse telemetry query") from exc
    if len(statements) != 1:
        raise QueryRejectedError("exactly one statement is required")
    if statements[0].type != duckdb.StatementType.SELECT:
        raise QueryRejectedError("only SELECT statements are allowed on the query endpoint")
    try:
        serialized = parser.execute("SELECT json_serialize_sql($sql)", {"sql": sql}).fetchone()
        parsed = json.loads(serialized[0]) if serialized else {}
    except (duckdb.Error, json.JSONDecodeError, TypeError) as exc:
        raise QueryRejectedError("could not parse telemetry query") from exc
    if parsed.get("error") or len(parsed.get("statements", [])) != 1:
        raise QueryRejectedError("could not parse telemetry query")
    return parsed["statements"][0]


def assert_query_safe(parser: duckdb.DuckDBPyConnection, sql: str) -> None:
    """Reject anything except SELECTs over allow-listed local telemetry tables."""
    statement = _parse_select(parser, sql)
    cte_names: set[str] = set()
    for node in _walk_ast(statement):
        cte_map = node.get("cte_map")
        if isinstance(cte_map, dict):
            for entry in cte_map.get("map", []):
                if isinstance(entry, dict) and isinstance(entry.get("key"), str):
                    cte_names.add(entry["key"])

    for node in _walk_ast(statement):
        node_type = node.get("type")
        if node_type == "TABLE_FUNCTION":
            raise QueryRejectedError("table functions are not allowed on the query endpoint")
        if node_type == "BASE_TABLE":
            table = node.get("table_name")
            schema = node.get("schema_name") or ""
            catalog = node.get("catalog_name") or ""
            if not catalog and not schema and table in cte_names:
                continue
            if catalog or schema not in _ALLOWED_SCHEMAS or table not in _ALLOWED_QUERY_TABLES:
                raise QueryRejectedError("query references a table outside the approved telemetry schema")
        if node.get("class") == "FUNCTION":
            function = str(node.get("function_name") or "").lower()
            if (
                function in _BLOCKED_SCALAR_FUNCTIONS
                or "secret" in function
                or function.startswith(("read_", "http_", "url_"))
            ):
                raise QueryRejectedError("query uses a restricted function")


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
        # Parsing is isolated from the data connection and cannot load extensions
        # or access files/network even if a future parser feature binds expressions.
        self._parser = duckdb.connect(
            ":memory:",
            config={
                "enable_external_access": "false",
                "autoinstall_known_extensions": "false",
                "autoload_known_extensions": "false",
                "allow_unsigned_extensions": "false",
            },
        )
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
        assert_query_safe(self._parser, sql)
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
                optic.error("telemetry query did not stop after interrupt")
            optic.warning("telemetry query interrupted after {:.0f}ms", budget * 1000)
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
        self._parser.close()
        self._executor.shutdown(wait=False, cancel_futures=True)
