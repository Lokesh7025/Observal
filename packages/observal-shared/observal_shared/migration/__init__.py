# SPDX-FileCopyrightText: 2026 Hari Srinivasan <harisrini21@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Shared Migration Service: export, import, and validation for PostgreSQL and telemetry.

Public API entry points:
    export_pg          — PostgreSQL snapshot export to .tar.gz archive
    export_telemetry   — DuckDB telemetry store export to checksummed Parquet chunks
    export_ch          — Legacy ClickHouse export (cutover source only)
    import_pg          — Import PG archive into target database
    import_telemetry   — Import telemetry Parquet chunks into a DuckDB store (idempotent)
    validate_pg        — Validate PG archive checksums and row counts
    validate_telemetry — Validate telemetry checksums, row counts, and FK references
    run_cutover        — ClickHouse -> DuckDB cutover: export, import, rebuild, verify

This module contains NO typer, NO rich, and NO typer.Exit.
Progress is reported through an injected ProgressReporter protocol.
Errors are raised as plain domain exceptions.
"""

from observal_shared.migration.connections import ChConnParams, PgConnParams, TelemetryConnParams
from observal_shared.migration.constants import DEFAULT_PROJECT_ID
from observal_shared.migration.cutover import CutoverState, reverse_cutover, run_cutover
from observal_shared.migration.exceptions import (
    ArtifactValidationError,
    ChecksumMismatchError,
    ConnectionFailedError,
    MigrationError,
    PrerequisiteError,
)
from observal_shared.migration.legacy_clickhouse_export import export_ch
from observal_shared.migration.pg_export import export_pg
from observal_shared.migration.pg_import import import_pg
from observal_shared.migration.progress import NullReporter, ProgressReporter
from observal_shared.migration.results import (
    ChecksumResult,
    ExportResult,
    ImportResult,
    TelemetryExportResult,
    TelemetryImportResult,
    TelemetryValidationResult,
    ValidationResult,
)
from observal_shared.migration.telemetry_export import export_telemetry
from observal_shared.migration.telemetry_import import import_telemetry
from observal_shared.migration.validation import validate_pg, validate_telemetry

__all__ = [
    "DEFAULT_PROJECT_ID",
    "ArtifactValidationError",
    "ChConnParams",
    "ChecksumMismatchError",
    "ChecksumResult",
    "ConnectionFailedError",
    "CutoverState",
    # Results
    "ExportResult",
    "ImportResult",
    # Exceptions
    "MigrationError",
    "NullReporter",
    # Connection params
    "PgConnParams",
    "PrerequisiteError",
    # Progress
    "ProgressReporter",
    "TelemetryConnParams",
    "TelemetryExportResult",
    "TelemetryImportResult",
    "TelemetryValidationResult",
    "ValidationResult",
    "export_ch",
    # Entry points
    "export_pg",
    "export_telemetry",
    "import_pg",
    "import_telemetry",
    "reverse_cutover",
    "run_cutover",
    "validate_pg",
    "validate_telemetry",
]
