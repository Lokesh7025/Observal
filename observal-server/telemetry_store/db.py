# SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""DuckDB connection ownership, schema application, and row serialisation."""

from __future__ import annotations

import datetime as dt
import decimal
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

import duckdb
from filelock import FileLock, Timeout
from loguru import logger as optic

from telemetry_store.sql import sql_string_literal

if TYPE_CHECKING:
    from telemetry_store.settings import TelemetrySettings

SCHEMA_DIR = Path(__file__).resolve().parent / "schema"
BASELINE_VERSION = "001_baseline"

FORBIDDEN_DDL = ("INSERT OR REPLACE", "PRIMARY KEY", "UNIQUE", "CREATE INDEX", "CREATE UNIQUE INDEX")


class SingleWriterViolationError(RuntimeError):
    """Another process already owns the telemetry database file."""


def _strip_sql_comments(sql: str) -> str:
    return "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))


def split_statements(sql: str) -> list[str]:
    """Split a SQL script into statements (quote-aware)."""
    statements: list[str] = []
    current: list[str] = []
    quote: str | None = None
    for char in _strip_sql_comments(sql):
        current.append(char)
        if quote:
            if char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
            continue
        if char == ";":
            stmt = "".join(current).strip().rstrip(";").strip()
            if stmt:
                statements.append(stmt)
            current = []
    tail = "".join(current).strip()
    if tail:
        statements.append(tail)
    return statements


def format_timestamp(value: dt.datetime) -> str:
    """ClickHouse-compatible ``YYYY-MM-DD HH:MM:SS.mmm`` in UTC."""
    if value.tzinfo is not None:
        value = value.astimezone(dt.UTC).replace(tzinfo=None)
    return value.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def to_json_value(value: Any) -> Any:
    """Convert DuckDB scalar values to JSON-serialisable values."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dt.datetime):
        return format_timestamp(value)
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, dt.time):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return value.total_seconds()
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if isinstance(value, (list, tuple)):
        return [to_json_value(v) for v in value]
    if isinstance(value, dict):
        return {str(k): to_json_value(v) for k, v in value.items()}
    return str(value)


def rows_to_dicts(columns: list[str], rows: list[tuple]) -> list[dict[str, Any]]:
    return [{col: to_json_value(val) for col, val in zip(columns, row, strict=True)} for row in rows]


class Database:
    """Owns the single DuckDB connection and the on-disk lock."""

    def __init__(self, settings: TelemetrySettings):
        self.settings = settings
        self._lock: FileLock | None = None
        self.conn: duckdb.DuckDBPyConnection | None = None

    @property
    def is_memory(self) -> bool:
        return str(self.settings.db_path) == ":memory:"

    def open(self) -> duckdb.DuckDBPyConnection:
        if self.conn is not None:
            return self.conn
        if not self.is_memory:
            self.settings.db_path.parent.mkdir(parents=True, exist_ok=True)
            self.settings.temp_dir.mkdir(parents=True, exist_ok=True)
            self._lock = FileLock(str(self.settings.db_path) + ".lock")
            try:
                self._lock.acquire(timeout=0.5)
            except Timeout as exc:
                raise SingleWriterViolationError(
                    f"telemetry database {self.settings.db_path} is already owned by another process"
                ) from exc
        conn = duckdb.connect(str(self.settings.db_path))
        conn.execute("SET TimeZone = 'UTC'")
        conn.execute(f"SET memory_limit = '{self.settings.memory_limit}'")
        conn.execute(f"SET threads = {int(self.settings.threads)}")
        conn.execute("SET preserve_insertion_order = false")
        # Trusted export/import/backup jobs require filesystem access, but extension
        # discovery and loading are never needed by this service. The query endpoint
        # is additionally constrained by structural AST validation.
        conn.execute("SET autoinstall_known_extensions = false")
        conn.execute("SET autoload_known_extensions = false")
        conn.execute("SET allow_unsigned_extensions = false")
        if not self.is_memory:
            conn.execute(f"SET temp_directory = {sql_string_literal(str(self.settings.temp_dir))}")
            conn.execute(f"SET checkpoint_threshold = '{self.settings.checkpoint_threshold}'")
        # All service filesystem I/O is performed by Python against server-owned
        # paths. DuckDB itself never needs arbitrary filesystem or URL access.
        conn.execute("SET enable_external_access = false")
        self.conn = conn
        optic.info(
            "telemetry database opened (path={}, memory_limit={}, threads={})",
            self.settings.db_path,
            self.settings.memory_limit,
            self.settings.threads,
        )
        return conn

    def close(self) -> None:
        if self.conn is not None:
            try:
                if not self.is_memory:
                    self.conn.execute("CHECKPOINT")
            except duckdb.Error as exc:
                optic.warning("checkpoint on close failed: {}", exc)
            self.conn.close()
            self.conn = None
        if self._lock is not None:
            self._lock.release()
            self._lock = None

    # ── Schema ────────────────────────────────────────────────────────

    def apply_schema(self) -> list[str]:
        """Apply pending schema files. Returns the versions applied in this call."""
        conn = self.open()
        applied: list[str] = []
        files = sorted(SCHEMA_DIR.glob("*.sql"))
        if not files:
            raise RuntimeError(f"no schema files found in {SCHEMA_DIR}")
        # The ledger table is created by the baseline itself; probe first.
        has_ledger = bool(
            conn.execute(
                "SELECT count(*) FROM information_schema.tables WHERE table_name = 'schema_migrations'"
            ).fetchone()[0]
        )
        existing: set[str] = set()
        if has_ledger:
            existing = {row[0] for row in conn.execute("SELECT version FROM schema_migrations").fetchall()}
        for path in files:
            version = path.stem
            if version in existing:
                continue
            script = path.read_text()
            upper = script.upper()
            for forbidden in FORBIDDEN_DDL:
                if forbidden in upper:
                    raise RuntimeError(f"schema {path.name} contains forbidden DDL: {forbidden}")
            statements = split_statements(script)
            optic.info("applying telemetry schema {} ({} statements)", path.name, len(statements))
            conn.execute("BEGIN")
            try:
                for stmt in statements:
                    conn.execute(stmt)
                conn.execute(
                    "INSERT INTO schema_migrations (version, name) VALUES ($v, $n)",
                    {"v": version, "n": path.name},
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            applied.append(version)
        return applied

    def schema_version(self) -> str | None:
        conn = self.open()
        row = conn.execute("SELECT max(version) FROM schema_migrations").fetchone()
        return row[0] if row else None

    # ── Introspection ─────────────────────────────────────────────────

    def table_counts(self) -> dict[str, int]:
        conn = self.open()
        names = [
            r[0]
            for r in conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'main' AND table_type = 'BASE TABLE' ORDER BY 1"
            ).fetchall()
        ]
        counts: dict[str, int] = {}
        for name in names:
            counts[name] = int(conn.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0])
        return counts

    def file_sizes(self) -> tuple[int, int]:
        if self.is_memory:
            return 0, 0
        db_path = self.settings.db_path
        wal = Path(str(db_path) + ".wal")
        return (
            db_path.stat().st_size if db_path.exists() else 0,
            wal.stat().st_size if wal.exists() else 0,
        )
