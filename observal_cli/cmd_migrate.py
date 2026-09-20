# SPDX-FileCopyrightText: 2026 Apoorv Garg <apoorvgarg.21@gmail.com>
# SPDX-FileCopyrightText: 2026 Hari Srinivasan <harisrini21@gmail.com>
# SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com>
# SPDX-FileCopyrightText: 2026 Naraen Rammoorthi <naraen13@gmail.com>
# SPDX-FileCopyrightText: 2026 Shaan Narendran <shaannaren06@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Portable PostgreSQL and telemetry migration commands.

This module provides the CLI commands for data migration. All core logic is
delegated to the shared `observal_shared.migration` package. This module handles
only CLI-specific concerns: human and JSON output, categorized errors, and progress reporting.
"""

from __future__ import annotations

import asyncio
import tarfile
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from rich import print as rprint
from rich.markup import escape

from observal_cli.errors import ErrorCategory, fail
from observal_cli.render import OutputMode, output_json, spinner

# ── Shared service imports ───────────────────────────────
from observal_shared.migration import (
    ChConnParams,
    ChecksumMismatchError,
    ConnectionFailedError,
    ExportResult,
    ImportResult,
    MigrationError,
    PgConnParams,
    PrerequisiteError,
    TelemetryConnParams,
    TelemetryExportResult,
    TelemetryImportResult,
    TelemetryValidationResult,
    ValidationResult,
    export_pg,
    export_telemetry,
    import_pg,
    import_telemetry,
    reverse_cutover,
    run_cutover,
    validate_pg,
    validate_telemetry,
)
from observal_shared.migration.connections import parse_clickhouse_url
from observal_shared.migration.constants import _UUID_RE  # noqa: F401, re-exported for backward compat

# ── RichProgressReporter ─────────────────────────────────


class RichProgressReporter:
    """CLI progress reporter that uses rich console output.

    Satisfies the ProgressReporter protocol defined in observal_shared.migration.progress.
    """

    def __init__(self) -> None:
        self._last_phase: str | None = None

    async def update(self, *, phase: str, pct: int, message: str) -> None:
        """Report progress via rich console output."""
        if phase != self._last_phase:
            if self._last_phase is not None:
                rprint()  # Blank line between phases
            self._last_phase = phase
        rprint(f"  [dim][{pct:3d}%][/dim] {escape(message)}")


class NullProgressReporter:
    """Discard migration progress in finite JSON mode."""

    async def update(self, *, phase: str, pct: int, message: str) -> None:
        return None


# ── CLI-specific helpers ─────────────────────────────────


def _is_json(output: OutputMode) -> bool:
    return output == "json"


def _reporter(output: OutputMode) -> RichProgressReporter | NullProgressReporter:
    return NullProgressReporter() if _is_json(output) else RichProgressReporter()


def _progress(output: OutputMode, message: str):
    return nullcontext() if _is_json(output) else spinner(message)


def _require_pyarrow() -> None:
    """pyarrow is an optional dependency; tell the user how to install it."""
    try:
        import pyarrow  # noqa: F401
    except ImportError as error:
        fail(
            ErrorCategory.UNAVAILABLE,
            "Migration support is not installed.",
            operation="Load migration tools",
            resource="pyarrow",
            remediation="Install the CLI migrate extra and retry.",
            detail=repr(error),
        )


def _handle_migration_error(error: MigrationError, operation: str) -> None:
    """Convert a migration domain failure to the CLI error contract."""
    if isinstance(error, ChecksumMismatchError):
        category = ErrorCategory.VALIDATION
        message = "Migration checksum verification failed."
        remediation = "Re-export the source data and retry with the new artifact."
    elif isinstance(error, ConnectionFailedError):
        category = ErrorCategory.UNAVAILABLE
        message = "The migration database connection failed."
        remediation = "Check database availability and credentials, then retry."
    elif isinstance(error, PrerequisiteError):
        category = ErrorCategory.VALIDATION
        message = "A migration prerequisite is missing or invalid."
        remediation = "Complete and validate the previous migration phase, then retry."
    else:
        category = ErrorCategory.UNAVAILABLE
        message = "The migration operation failed."
        remediation = "Check the source artifact and database health, then retry."
    fail(
        category,
        message,
        operation=operation,
        resource="migration data",
        remediation=remediation,
        detail=type(error).__name__,
    )


def _warn_clickhouse_cleartext(url: str, output: OutputMode) -> None:
    """Emit a warning when using unencrypted HTTP transport with credentials."""
    try:
        http_url, _db, _user, password = parse_clickhouse_url(url)
    except ValueError as error:
        fail(
            ErrorCategory.VALIDATION,
            "The ClickHouse connection URL is invalid.",
            operation="Validate migration connection",
            resource="ClickHouse URL",
            remediation="Provide a clickhouse:// or clickhouses:// URL and retry.",
            detail=type(error).__name__,
        )
    if not _is_json(output) and http_url.startswith("http://") and password:
        rprint(
            "[yellow]⚠  ClickHouse credentials will be sent over unencrypted HTTP.[/yellow]\n"
            "[yellow]   Use clickhouses:// (TLS) for production environments.[/yellow]"
        )


# ── Typer app ────────────────────────────────────────────

migrate_app = typer.Typer(
    help=(
        "Portable PostgreSQL and telemetry migration tools\n\n"
        "Examples:\n"
        "  observal server migrate export --db-url postgresql://localhost/observal --file backup.tar.gz\n"
        "  observal server migrate validate --archive backup.tar.gz --output json\n"
        "  observal server migrate export-telemetry --telemetry-url http://localhost:8125 --output-dir ./telemetry\n"
        "  observal server migrate telemetry-cutover --clickhouse-url clickhouse://localhost:8123/observal "
        "--telemetry-url http://localhost:8125 --artifact-dir ./cutover"
    )
)


def _telemetry_params(url: str, token: str | None) -> TelemetryConnParams:
    if not url.startswith(("http://", "https://")):
        fail(
            ErrorCategory.VALIDATION,
            "The telemetry store URL is invalid.",
            operation="Validate telemetry connection",
            resource="Telemetry URL",
            remediation="Provide an http:// or https:// URL for the telemetry store and retry.",
        )
    return TelemetryConnParams(url=url, token=token or "")


_TELEMETRY_URL_OPTION = typer.Option(
    ..., "--telemetry-url", envvar="TELEMETRY_URL", show_envvar=True, help="Telemetry store base URL"
)
_TELEMETRY_TOKEN_OPTION = typer.Option(
    None, "--telemetry-token", envvar="TELEMETRY_TOKEN", show_envvar=True, help="Telemetry store bearer token"
)


# ── Export command ───────────────────────────────────────


@migrate_app.command("export")
def export_cmd(
    db_url: str = typer.Option(
        ..., "--db-url", envvar="DATABASE_URL", show_envvar=True, help="Source PostgreSQL connection string"
    ),
    file: Annotated[str | None, typer.Option("--file", "-f", help="Destination archive path")] = None,
    output: Annotated[
        OutputMode, typer.Option("--output", "-o", help="Output format: table or json")
    ] = OutputMode.table,
) -> None:
    """Export all PostgreSQL registry data to a portable archive.

    Connects to the source database, reads all tables in a consistent
    REPEATABLE READ snapshot, and writes JSONL files packed into a
    checksummed .tar.gz archive.

    The archive includes a manifest with SHA-256 checksums and the source
    Alembic migration version for compatibility verification on import.

    Examples:
        observal server migrate export --db-url postgresql://localhost/observal --file backup.tar.gz
        observal server migrate export --db-url postgresql://localhost/observal --file backup.tar.gz --output json
    """
    _require_pyarrow()
    output_path = Path(file or f"observal-export-{datetime.now(UTC):%Y%m%d-%H%M%S}.tar.gz").expanduser()
    if output_path.exists():
        fail(
            ErrorCategory.CONFLICT,
            "The migration archive already exists.",
            operation="Export PostgreSQL registry",
            resource=str(output_path),
            remediation="Choose a new --file destination.",
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not _is_json(output):
        rprint(f"[bold]Exporting to:[/bold] {escape(str(output_path))}")
    try:
        with _progress(output, "Connecting to source database..."):
            result: ExportResult = asyncio.run(export_pg(PgConnParams(dsn=db_url), output_path, _reporter(output)))
    except MigrationError as error:
        _handle_migration_error(error, "Export PostgreSQL registry")

    output_path.chmod(0o600)
    sidecar = output_path.parent / f"{output_path.name.removesuffix('.tar.gz').removesuffix('.tgz')}.manifest.json"
    if not sidecar.is_file():
        fail(
            ErrorCategory.UNAVAILABLE,
            "The PostgreSQL export did not produce its migration manifest.",
            operation="Export PostgreSQL registry",
            resource=str(sidecar),
            remediation="Remove the archive and retry the export.",
        )
    sidecar.chmod(0o600)
    payload = {
        "archive": str(output_path),
        "manifest": str(sidecar) if sidecar.exists() else None,
        "migration_id": result.migration_id,
        "table_counts": result.table_counts,
        "total_rows": result.total_rows,
        "size_bytes": output_path.stat().st_size,
        "duration_seconds": result.duration_seconds,
    }
    if _is_json(output):
        output_json(payload)
        return
    rprint("\n[bold green]✓ Export complete[/bold green]")
    rprint(f"  Archive:    {escape(result.archive_path)}")
    rprint(f"  Migration:  {escape(result.migration_id)}")
    rprint(f"  Tables:     {len(result.table_counts)}")
    rprint(f"  Rows:       {result.total_rows:,}")
    rprint(f"  Size:       {payload['size_bytes'] / (1024 * 1024):.1f} MB")
    rprint(f"  Duration:   {result.duration_seconds:.1f}s")
    rprint("\n[yellow]Archive may contain sensitive registry data. Store it securely.[/yellow]")


# ── Import command ───────────────────────────────────────


@migrate_app.command("import")
def import_cmd(
    db_url: str = typer.Option(
        ..., "--db-url", envvar="TARGET_DATABASE_URL", show_envvar=True, help="Target PostgreSQL connection string"
    ),
    archive: str = typer.Option(..., "--archive", "-a", help="Path to .tar.gz archive"),
    output: Annotated[
        OutputMode, typer.Option("--output", "-o", help="Output format: table or json")
    ] = OutputMode.table,
) -> None:
    """Import a migration archive into the target database.

    Verifies checksums before inserting any data. Uses ON CONFLICT DO NOTHING
    for idempotent imports: existing rows are skipped, not overwritten.

    Examples:
        observal server migrate import --db-url postgresql://localhost/observal --archive backup.tar.gz
        observal server migrate import --db-url postgresql://localhost/observal --archive backup.tar.gz --output json
    """
    _require_pyarrow()
    archive_path = Path(archive).expanduser()
    if not archive_path.is_file():
        fail(
            ErrorCategory.NOT_FOUND,
            "The PostgreSQL migration archive was not found.",
            operation="Import PostgreSQL registry",
            resource=str(archive_path),
            remediation="Provide an existing archive and retry.",
        )
    if not tarfile.is_tarfile(archive_path):
        fail(
            ErrorCategory.VALIDATION,
            "The PostgreSQL migration archive is invalid.",
            operation="Import PostgreSQL registry",
            resource=str(archive_path),
            remediation="Provide a validated export archive and retry.",
        )
    if not _is_json(output):
        rprint(f"[bold]Importing from:[/bold] {escape(str(archive_path))}")
    try:
        with _progress(output, "Importing..."):
            result: ImportResult = asyncio.run(import_pg(PgConnParams(dsn=db_url), archive_path, _reporter(output)))
    except MigrationError as error:
        _handle_migration_error(error, "Import PostgreSQL registry")

    payload = {
        "migration_id": result.migration_id,
        "tables_imported": result.tables_imported,
        "rows_inserted": result.rows_inserted,
        "rows_skipped": result.rows_skipped,
        "total_inserted": sum(result.rows_inserted.values()),
        "total_skipped": sum(result.rows_skipped.values()),
        "duration_seconds": result.duration_seconds,
        "warnings": result.warnings,
    }
    if _is_json(output):
        output_json(payload)
        return
    rprint("\n[bold green]✓ Import complete[/bold green]")
    rprint(f"  Migration:  {escape(result.migration_id)}")
    rprint(f"  Tables:     {result.tables_imported}")
    rprint(f"  Inserted:   {payload['total_inserted']:,}")
    rprint(f"  Skipped:    {payload['total_skipped']:,}")
    rprint(f"  Duration:   {result.duration_seconds:.1f}s")
    if result.warnings:
        rprint("\n[yellow]Warnings:[/yellow]")
        for warning in result.warnings:
            rprint(f"  [yellow]⚠[/yellow]  {escape(warning)}")


# ── Validate command ─────────────────────────────────────


@migrate_app.command("validate")
def validate_cmd(
    archive: str = typer.Option(..., "--archive", "-a", help="Path to .tar.gz archive"),
    db_url: str | None = typer.Option(
        None,
        "--db-url",
        envvar="TARGET_DATABASE_URL",
        show_envvar=True,
        help="Optional database for cross-validation",
    ),
    output: Annotated[
        OutputMode, typer.Option("--output", "-o", help="Output format: table or json")
    ] = OutputMode.table,
) -> None:
    """Validate archive integrity and optionally compare against a database.

    Checks SHA-256 checksums for every table file in the archive. If --db-url
    is provided, also compares row counts between the archive and the live
    database to detect drift or partial imports.

    Examples:
        observal server migrate validate --archive backup.tar.gz
        observal server migrate validate --archive backup.tar.gz --output json
    """
    _require_pyarrow()
    archive_path = Path(archive).expanduser()
    if not archive_path.is_file():
        fail(
            ErrorCategory.NOT_FOUND,
            "The PostgreSQL migration archive was not found.",
            operation="Validate PostgreSQL migration",
            resource=str(archive_path),
            remediation="Provide an existing archive and retry.",
        )
    if not tarfile.is_tarfile(archive_path):
        fail(
            ErrorCategory.VALIDATION,
            "The PostgreSQL migration archive is invalid.",
            operation="Validate PostgreSQL migration",
            resource=str(archive_path),
            remediation="Provide an export archive and retry.",
        )
    try:
        with _progress(output, "Validating archive..."):
            result: ValidationResult = asyncio.run(
                validate_pg(PgConnParams(dsn=db_url) if db_url else None, archive_path, _reporter(output))
            )
    except MigrationError as error:
        _handle_migration_error(error, "Validate PostgreSQL migration")

    checksum_results = [
        {
            "table": item.table_name,
            "expected_checksum": item.expected_checksum,
            "actual_checksum": item.actual_checksum,
            "passed": item.passed,
        }
        for item in result.checksum_results
    ]
    if not result.archive_valid:
        fail(
            ErrorCategory.VALIDATION,
            "PostgreSQL migration checksum validation failed.",
            operation="Validate PostgreSQL migration",
            resource=str(archive_path),
            remediation="Discard the archive and export it again.",
        )
    comparisons = {
        table: {"archive_rows": counts[0], "database_rows": counts[1], "matches": counts[0] == counts[1]}
        for table, counts in (result.cross_db_results or {}).items()
    }
    payload = {
        "archive": str(archive_path),
        "valid": True,
        "checksums": checksum_results,
        "row_counts": comparisons,
        "row_count_mismatches": sum(not item["matches"] for item in comparisons.values()),
    }
    if _is_json(output):
        output_json(payload)
        return
    rprint("\n[bold]Checksum verification:[/bold]")
    for item in checksum_results:
        rprint(f"  {'[green]✓[/green]' if item['passed'] else '[red]✗[/red]'} {escape(str(item['table']))}")
    rprint("\n[green]✓ All checksums valid[/green]")
    if comparisons:
        rprint("\n[bold]Row count comparison:[/bold]")
        for table, item in comparisons.items():
            status = "[green]✓[/green]" if item["matches"] else "[yellow]≠[/yellow]"
            rprint(f"  {status} {escape(table)}: archive={item['archive_rows']}, db={item['database_rows']}")


# ── Export telemetry command ─────────────────────────────


@migrate_app.command("export-telemetry")
def export_telemetry_cmd(
    telemetry_url: str = _TELEMETRY_URL_OPTION,
    telemetry_token: str | None = _TELEMETRY_TOKEN_OPTION,
    output_dir: str = typer.Option(..., "--output-dir", help="New directory for exported Parquet files"),
    migration_id: str | None = typer.Option(None, "--migration-id", help="Identifier recorded in the manifest"),
    since: str | None = typer.Option(
        None, "--since", help="Only export rows written after this UTC timestamp (YYYY-MM-DD HH:MM:SS)"
    ),
    output: Annotated[
        OutputMode, typer.Option("--output", "-o", help="Output format: table or json")
    ] = OutputMode.table,
) -> None:
    """Export telemetry store data to checksummed Parquet files.

    The export runs as a job inside the telemetry store (no interactive
    timeout); chunks are downloaded and verified against the manifest.

    Examples:
        observal server migrate export-telemetry --telemetry-url http://localhost:8125 --output-dir ./telemetry-export
    """
    _require_pyarrow()
    destination = Path(output_dir).expanduser()
    if destination.exists() and any(destination.iterdir()):
        fail(
            ErrorCategory.CONFLICT,
            "The telemetry export directory already exists and is not empty.",
            operation="Export telemetry",
            resource=str(destination),
            remediation="Choose a new --output-dir so failed exports can be cleaned safely.",
        )
    params = _telemetry_params(telemetry_url, telemetry_token)
    if not _is_json(output):
        rprint(f"[bold]Exporting telemetry to:[/bold] {escape(str(destination))}")
    try:
        result: TelemetryExportResult = asyncio.run(
            export_telemetry(
                params,
                destination,
                _reporter(output),
                migration_id=migration_id or f"export-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}",
                since=since,
            )
        )
    except MigrationError as error:
        _handle_migration_error(error, "Export telemetry")

    payload = {
        "directory": result.output_dir,
        "migration_id": result.migration_id,
        "tables": result.table_results,
        "total_rows": result.total_rows,
        "size_bytes": result.total_size_bytes,
        "duration_seconds": result.duration_seconds,
    }
    if _is_json(output):
        output_json(payload)
        return
    rprint("\n[bold green]✓ Telemetry export complete[/bold green]")
    rprint(f"  Directory:  {escape(result.output_dir)}")
    rprint(f"  Migration:  {escape(result.migration_id)}")
    rprint(f"  Rows:       {result.total_rows:,}")
    rprint(f"  Size:       {result.total_size_bytes / (1024 * 1024):.1f} MB")
    rprint(f"  Duration:   {result.duration_seconds:.1f}s")
    rprint("\n[yellow]Parquet files may contain sensitive trace data. Store them securely.[/yellow]")


# ── Import telemetry command ─────────────────────────────


@migrate_app.command("import-telemetry")
def import_telemetry_cmd(
    telemetry_url: str = _TELEMETRY_URL_OPTION,
    telemetry_token: str | None = _TELEMETRY_TOKEN_OPTION,
    input_dir: str = typer.Option(..., "--input-dir", help="Directory containing Parquet files and manifest"),
    no_rebuild: bool = typer.Option(False, "--no-rebuild", help="Skip rebuilding session summaries and checkpoints"),
    output: Annotated[
        OutputMode, typer.Option("--output", "-o", help="Output format: table or json")
    ] = OutputMode.table,
) -> None:
    """Import Parquet telemetry files into the telemetry store.

    Accepts exports from a telemetry store or from the legacy ClickHouse
    exporter. Every chunk is verified and imported idempotently: re-running
    after an interruption never duplicates rows. Derived tables (session
    summaries and checkpoints) are rebuilt from the imported events.

    Examples:
        observal server migrate import-telemetry --telemetry-url http://localhost:8125 --input-dir ./telemetry-export
    """
    _require_pyarrow()
    input_path = Path(input_dir).expanduser()
    if not input_path.is_dir():
        fail(
            ErrorCategory.NOT_FOUND,
            "The telemetry migration directory was not found.",
            operation="Import telemetry",
            resource=str(input_path),
            remediation="Provide an existing telemetry export directory.",
        )
    params = _telemetry_params(telemetry_url, telemetry_token)
    if not _is_json(output):
        rprint(f"[bold]Importing telemetry from:[/bold] {escape(str(input_path))}")
    try:
        result: TelemetryImportResult = asyncio.run(
            import_telemetry(params, input_path, _reporter(output), rebuild=not no_rebuild)
        )
    except MigrationError as error:
        _handle_migration_error(error, "Import telemetry")

    payload = {
        "migration_id": result.migration_id,
        "tables_imported": result.tables_imported,
        "rows_imported": result.rows_imported,
        "failed_files": result.failed_files,
        "derived_rebuild": result.derived_rebuild,
        "duration_seconds": result.duration_seconds,
    }
    if _is_json(output):
        output_json(payload)
        return
    rprint("\n[bold green]✓ Telemetry import complete[/bold green]")
    rprint(f"  Migration:  {escape(result.migration_id)}")
    rprint(f"  Tables:     {', '.join(map(escape, sorted(result.tables_imported)))}")
    rprint(f"  Rows:       {result.rows_imported:,}")
    if result.derived_rebuild:
        rprint(f"  Rebuilt:    {result.derived_rebuild.get('sessions', 0):,} session summaries")
    rprint(f"  Duration:   {result.duration_seconds:.1f}s")


# ── Validate telemetry command ───────────────────────────


@migrate_app.command("validate-telemetry")
def validate_telemetry_cmd(
    input_dir: str = typer.Option(..., "--input-dir", help="Directory containing Parquet files"),
    telemetry_url: str | None = typer.Option(
        None,
        "--telemetry-url",
        envvar="TELEMETRY_URL",
        show_envvar=True,
        help="Telemetry store for row count comparison",
    ),
    telemetry_token: str | None = _TELEMETRY_TOKEN_OPTION,
    target_db_url: str | None = typer.Option(
        None,
        "--target-db-url",
        envvar="TARGET_DATABASE_URL",
        show_envvar=True,
        help="Target PostgreSQL for FK validation",
    ),
    output: Annotated[
        OutputMode, typer.Option("--output", "-o", help="Output format: table or json")
    ] = OutputMode.table,
) -> None:
    """Validate telemetry Parquet files and optionally check FK references.

    Verifies SHA-256 checksums and row counts for every chunk. Optionally
    compares table counts against a telemetry store and checks foreign key
    references (agent_id, user_id) against PostgreSQL.

    Examples:
        observal server migrate validate-telemetry --input-dir ./telemetry-export
        observal server migrate validate-telemetry --input-dir ./telemetry-export --telemetry-url http://localhost:8125
    """
    _require_pyarrow()
    input_path = Path(input_dir).expanduser()
    if not input_path.is_dir():
        fail(
            ErrorCategory.NOT_FOUND,
            "The telemetry migration directory was not found.",
            operation="Validate telemetry",
            resource=str(input_path),
            remediation="Provide an existing telemetry export directory.",
        )
    if not _is_json(output):
        rprint(f"[bold]Validating telemetry in:[/bold] {escape(str(input_path))}")
    try:
        result: TelemetryValidationResult = asyncio.run(
            validate_telemetry(
                _telemetry_params(telemetry_url, telemetry_token) if telemetry_url else None,
                PgConnParams(dsn=target_db_url) if target_db_url else None,
                input_path,
                _reporter(output),
            )
        )
    except MigrationError as error:
        _handle_migration_error(error, "Validate telemetry")

    if not result.checksums_valid:
        fail(
            ErrorCategory.VALIDATION,
            "Telemetry checksum validation failed.",
            operation="Validate telemetry",
            resource=str(input_path),
            remediation="Discard the export and create it again.",
        )
    row_counts = {
        table: {"manifest_rows": counts[0], "database_rows": counts[1], "matches": counts[1] >= counts[0]}
        for table, counts in (result.row_count_results or {}).items()
    }
    orphan_groups = {
        key: value
        for key, value in (result.fk_results or {}).items()
        if not key.endswith("_truncated") and isinstance(value, list) and value
    }
    payload = {
        "directory": str(input_path),
        "valid": True,
        "checksums": result.checksum_results,
        "row_counts": row_counts,
        "row_count_mismatches": sum(not item["matches"] for item in row_counts.values()),
        "foreign_keys": result.fk_results or {},
        "orphan_groups": len(orphan_groups),
    }
    if _is_json(output):
        output_json(payload)
        return
    rprint("\n[bold]Checksum verification:[/bold]")
    for filename, passed in result.checksum_results.items():
        status = "[green]✓[/green]" if passed else "[red]✗[/red]"
        rprint(f"  {status} {escape(filename)}")
    rprint("\n[green]✓ All checksums valid[/green]")
    if row_counts:
        rprint("\n[bold]Row count comparison:[/bold]")
        for table, item in row_counts.items():
            status = "[green]✓[/green]" if item["matches"] else "[yellow]≠[/yellow]"
            rprint(f"  {status} {escape(table)}: manifest={item['manifest_rows']}, store={item['database_rows']}")
    if result.fk_results:
        rprint("\n[bold]FK validation:[/bold]")
        for key, value in result.fk_results.items():
            if not key.endswith("_truncated") and isinstance(value, list):
                rprint(
                    f"  {'[yellow]⚠[/yellow]' if value else '[green]✓[/green]'} {escape(key)}: {len(value)} orphaned"
                )


# ── ClickHouse -> DuckDB cutover ─────────────────────────


@migrate_app.command("telemetry-cutover")
def telemetry_cutover_cmd(
    clickhouse_url: str = typer.Option(
        ...,
        "--clickhouse-url",
        envvar="CLICKHOUSE_URL",
        show_envvar=True,
        help="Legacy ClickHouse connection string (source)",
    ),
    telemetry_url: str = _TELEMETRY_URL_OPTION,
    telemetry_token: str | None = _TELEMETRY_TOKEN_OPTION,
    artifact_dir: str = typer.Option(..., "--artifact-dir", help="Directory for chunks, state, and verification"),
    resume: bool = typer.Option(False, "--resume", help="Continue an interrupted cutover from its state file"),
    verify_only: bool = typer.Option(False, "--verify-only", help="Re-run verification for a completed cutover"),
    reverse: bool = typer.Option(
        False, "--reverse", help="Export telemetry store rows (for loading back into ClickHouse on rollback)"
    ),
    since: str | None = typer.Option(None, "--since", help="With --reverse: only rows written after this timestamp"),
    spot_check: int = typer.Option(100, "--spot-check", min=0, help="Sessions to compare event-by-event"),
    output: Annotated[
        OutputMode, typer.Option("--output", "-o", help="Output format: table or json")
    ] = OutputMode.table,
) -> None:
    """Backfill a DuckDB telemetry store from a legacy ClickHouse installation.

    Switch-then-backfill: the upgraded server already writes to the telemetry
    store; this command copies ClickHouse history into it while ingest keeps
    running. Steps: preflight, chunked export with checksums, idempotent import,
    derived-table rebuild, then verification (row counts + per-session spot
    checks). Nothing is deleted on either side; re-run with --resume after an
    interruption. ClickHouse is retired separately with 'observal server
    retire-clickhouse'.

    Examples:
        observal server migrate telemetry-cutover --clickhouse-url clickhouse://default:pw@localhost:8123/observal \\
            --telemetry-url http://localhost:8125 --artifact-dir ./cutover
        observal server migrate telemetry-cutover ... --artifact-dir ./cutover --resume
        observal server migrate telemetry-cutover ... --artifact-dir ./cutover --reverse --since "2026-06-01 00:00:00"
    """
    _require_pyarrow()
    _warn_clickhouse_cleartext(clickhouse_url, output)
    params = _telemetry_params(telemetry_url, telemetry_token)
    artifacts = Path(artifact_dir).expanduser()
    ch = ChConnParams(url=clickhouse_url)

    if reverse:
        try:
            result = asyncio.run(reverse_cutover(params, ch, artifacts, _reporter(output), since=since))
        except MigrationError as error:
            _handle_migration_error(error, "Reverse telemetry cutover")
        if _is_json(output):
            output_json(result)
            return
        rprint("\n[bold green]✓ Reverse export complete[/bold green]")
        rprint(f"  Directory:  {escape(result['output_dir'])}")
        rprint(f"  Rows:       {result['rows']:,}")
        rprint(
            "\nLoad the Parquet chunks into ClickHouse with "
            "[bold]INSERT INTO <table> FORMAT Parquet[/bold] to complete the rollback."
        )
        return

    if not _is_json(output):
        rprint(f"[bold]Cutover artifacts:[/bold] {escape(str(artifacts))}")
    try:
        state = asyncio.run(
            run_cutover(
                ch,
                params,
                artifacts,
                _reporter(output),
                resume=resume,
                verify_only=verify_only,
                spot_check_sessions=spot_check,
            )
        )
    except MigrationError as error:
        _handle_migration_error(error, "Telemetry cutover")

    payload = {
        "migration_id": state.migration_id,
        "phase": state.phase,
        "source_counts": state.source_counts,
        "verification": state.verification,
        "verified_at": state.verified_at,
    }
    if _is_json(output):
        output_json(payload)
        return
    rprint("\n[bold green]✓ Cutover complete and verified[/bold green]")
    rprint(f"  Migration:  {escape(state.migration_id)}")
    for table, count in state.source_counts.items():
        target = state.verification.get("target_counts", {}).get(table, 0)
        rprint(f"  {escape(table)}: source={count:,} store={target:,}")
    spot = state.verification.get("spot_check", {})
    rprint(f"  Spot check: {spot.get('sampled', 0)} sessions compared, {len(spot.get('mismatched', []))} mismatched")
    rprint(
        "\nClickHouse was not modified. When you are satisfied, run "
        "[bold]observal server retire-clickhouse[/bold] to stop it."
    )
