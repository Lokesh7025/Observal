# SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Policy guards for the telemetry store (acceptance A-15, A-16, A-17).

These encode the lessons from the abandoned prototype: no ClickHouse code
outside the legacy cutover path, exactly one DuckDB schema file with no
constraints or indexes, one process that opens the database, and no route
that swallows a telemetry failure into an empty result.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "observal-server"

# Files allowed to mention ClickHouse in code (the legacy cutover source path,
# migration constants that describe the ClickHouse export layout, and settings
# that keep old tfvars/compose profiles working).
CLICKHOUSE_ALLOWLIST = {
    "packages/observal-shared/observal_shared/migration/legacy_clickhouse_export.py",
    "packages/observal-shared/observal_shared/migration/constants.py",
    "packages/observal-shared/observal_shared/migration/connections.py",
    "packages/observal-shared/observal_shared/migration/cutover.py",
    "packages/observal-shared/observal_shared/migration/telemetry_manifest.py",
    "packages/observal-shared/observal_shared/migration/telemetry_import.py",
    "packages/observal-shared/observal_shared/migration/validation.py",
    "packages/observal-shared/observal_shared/migration/__init__.py",
    "observal_cli/cmd_migrate.py",
    "observal_cli/cmd_server.py",
    "observal_cli/server/constants.py",
    "observal_cli/server/config_gen.py",
    "observal_cli/server/deps.py",
    "observal_cli/server/orchestrator.py",
    "observal-server/alembic/versions/027_telemetry_migration_scope.py",
    "observal-server/telemetry_store/writer.py",
    "observal-server/telemetry_store/sql.py",
    "observal-server/telemetry_store/db.py",
    "observal-server/services/retention.py",
    "observal-server/services/telemetry/sql.py",
    "observal-server/services/telemetry/client.py",
    "observal-server/api/routes/insights.py",
}


def _py_files(*roots: Path):
    for root in roots:
        for path in root.rglob("*.py"):
            rel = path.relative_to(ROOT).as_posix()
            if (
                "/tests/" in f"/{rel}"
                or rel.startswith("tests/")
                or "/.venv/" in f"/{rel}"
                or ("alembic/versions/0" in rel and "027" not in rel)
            ):
                continue
            yield path, rel


def test_no_clickhouse_code_outside_the_legacy_cutover_path():
    offenders = []
    for path, rel in _py_files(SERVER, ROOT / "observal_cli", ROOT / "packages" / "observal-shared"):
        if rel in CLICKHOUSE_ALLOWLIST:
            continue
        text = path.read_text()
        # Strip comments and docstrings before matching so explanatory prose is fine.
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        code_lines = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom, ast.Call, ast.Attribute, ast.Name)):
                code_lines.add(getattr(node, "lineno", None))
        for lineno, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or lineno not in code_lines:
                continue
            if re.search(r"clickhouse", line, re.IGNORECASE):
                offenders.append(f"{rel}:{lineno}: {stripped[:100]}")
    assert not offenders, "ClickHouse references outside the allow-list:\n" + "\n".join(offenders)


def test_only_the_telemetry_store_opens_duckdb():
    offenders = []
    for path, rel in _py_files(SERVER, ROOT / "observal_cli", ROOT / "packages" / "observal-shared"):
        if rel.startswith("observal-server/telemetry_store/"):
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import) and any(a.name.split(".")[0] == "duckdb" for a in node.names):
                offenders.append(rel)
            if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "duckdb":
                offenders.append(rel)
    assert not offenders, f"only telemetry_store may import duckdb: {sorted(set(offenders))}"


def test_single_schema_file_without_constraints_or_indexes():
    schema_dir = SERVER / "telemetry_store" / "schema"
    files = sorted(p.name for p in schema_dir.glob("*.sql"))
    assert files == ["001_baseline.sql"], "the telemetry schema is one baseline file; edit it, do not add versions"
    sql = (schema_dir / files[0]).read_text().upper()
    for forbidden in ("INSERT OR REPLACE", "PRIMARY KEY", "UNIQUE", "CREATE INDEX", "FOREIGN KEY"):
        assert forbidden not in sql, f"forbidden DDL in baseline: {forbidden}"


def test_no_insert_or_replace_in_executed_sql():
    """The phrase may only appear in the guard that forbids it, never in SQL handed to DuckDB."""
    offenders = []
    for path, rel in _py_files(SERVER, ROOT / "packages" / "observal-shared"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "execute"
            ):
                continue
            src = ast.unparse(node)
            if re.search(r"INSERT\s+OR\s+REPLACE", src, re.IGNORECASE):
                offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, offenders
    guard = (SERVER / "telemetry_store" / "db.py").read_text()
    assert "INSERT OR REPLACE" in guard, "db.py must keep rejecting INSERT OR REPLACE in schema files"


def test_routes_never_return_empty_results_on_telemetry_failure():
    """No `except ...: return []/{}/None/0` directly around a telemetry query in API routes."""
    offenders = []
    for path in (SERVER / "api" / "routes").rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            body_src = ast.unparse(node.body)
            if not re.search(r"\b(tq|_telemetry_rows|query_one|scalar)\(", body_src):
                continue
            for handler in node.handlers:
                for stmt in handler.body:
                    if isinstance(stmt, ast.Return) and (
                        stmt.value is None
                        or (
                            isinstance(stmt.value, (ast.List, ast.Dict))
                            and not getattr(stmt.value, "elts", None)
                            and not getattr(stmt.value, "keys", None)
                        )
                        or (isinstance(stmt.value, ast.Constant) and stmt.value.value in (None, 0, [], {}))
                    ):
                        offenders.append(f"{path.relative_to(ROOT)}:{stmt.lineno}")
    assert not offenders, "telemetry failures swallowed into empty results:\n" + "\n".join(offenders)
