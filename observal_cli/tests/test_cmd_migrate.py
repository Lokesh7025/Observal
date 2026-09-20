# SPDX-FileCopyrightText: 2026 Hari Srinivasan <harisrini21@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Unit tests verifying CLI migrate commands invoke the shared Migration_Service correctly.

These tests mock the observal_shared.migration entry points and assert that the CLI
passes the correct arguments (PgConnParams, ChConnParams, paths, options) to
the shared core. No real database connections are made.

Requirements: 8.2, 8.3
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

from typer.testing import CliRunner

from observal_cli.cmd_migrate import migrate_app

runner = CliRunner()


# ── Helpers ────────────────────────────────────────────────────


def _make_export_result():
    """Build a mock ExportResult."""
    from observal_shared.migration.results import ExportResult

    return ExportResult(
        archive_path="/tmp/test.tar.gz",
        migration_id="mig-123",
        table_counts={"users": 10, "agents": 5},
        checksums={"users": "abc", "agents": "def"},
        duration_seconds=2.5,
        total_rows=15,
    )


def _make_import_result():
    """Build a mock ImportResult."""
    from observal_shared.migration.results import ImportResult

    return ImportResult(
        migration_id="mig-123",
        tables_imported=2,
        rows_inserted={"users": 10, "agents": 5},
        rows_skipped={"users": 0, "agents": 2},
        duration_seconds=3.0,
        warnings=[],
    )


def _make_validation_result():
    """Build a mock ValidationResult."""
    from observal_shared.migration.results import ChecksumResult, ValidationResult

    return ValidationResult(
        archive_valid=True,
        checksum_results=[ChecksumResult("users", "abc", "abc", True)],
        cross_db_results=None,
    )


def _make_telemetry_export_result():
    """Build a mock TelemetryExportResult."""
    from observal_shared.migration.results import TelemetryExportResult

    return TelemetryExportResult(
        output_dir="/tmp/telemetry",
        migration_id="mig-456",
        table_results={"traces": {"files": [], "row_count": 100}},
        total_rows=100,
        total_size_bytes=1024 * 1024,
        duration_seconds=5.0,
    )


def _make_telemetry_import_result():
    """Build a mock TelemetryImportResult."""
    from observal_shared.migration.results import TelemetryImportResult

    return TelemetryImportResult(
        migration_id="mig-456",
        tables_imported={"session_events": 100, "audit_log": 200},
        rows_imported=300,
        failed_files=[],
        duration_seconds=4.0,
    )


def _make_telemetry_validation_result():
    """Build a mock TelemetryValidationResult."""
    from observal_shared.migration.results import TelemetryValidationResult

    return TelemetryValidationResult(
        checksums_valid=True,
        checksum_results={"traces_2026-01.parquet": True},
        fk_results=None,
        row_count_results=None,
    )


# ── Export command tests ─────────────────────────────────────


class TestExportCommand:
    """Verify export_cmd passes correct args to export_pg."""

    @patch("observal_cli.cmd_migrate.export_pg", new_callable=AsyncMock)
    def test_export_passes_pg_conn_params(self, mock_export_pg, tmp_path):
        """export_pg receives PgConnParams with the --db-url DSN."""
        mock_export_pg.return_value = _make_export_result()
        output = tmp_path / "out.tar.gz"

        # The CLI checks output_path.stat().st_size after export_pg returns,
        # so we need the file to exist. Create it as a side effect of the mock.
        async def _fake_export(*args, **kwargs):
            output.write_bytes(b"\x00" * 1024)
            output.with_name("out.manifest.json").write_text("{}")
            return _make_export_result()

        mock_export_pg.side_effect = _fake_export

        result = runner.invoke(
            migrate_app,
            ["export", "--db-url", "postgresql://user:pass@myhost:5432/mydb", "--file", str(output)],
        )

        assert result.exit_code == 0, result.output
        mock_export_pg.assert_called_once()
        args = mock_export_pg.call_args
        # First arg: PgConnParams
        pg_params = args[0][0]
        assert pg_params.dsn == "postgresql://user:pass@myhost:5432/mydb"
        # Second arg: output path
        assert args[0][1] == output
        # Third arg: reporter (RichProgressReporter instance)
        assert hasattr(args[0][2], "update")


# ── Import command tests ─────────────────────────────────────


class TestImportCommand:
    """Verify import_cmd passes correct args to import_pg."""

    @patch("observal_cli.cmd_migrate.import_pg", new_callable=AsyncMock)
    def test_import_passes_pg_conn_params_and_archive(self, mock_import_pg, tmp_path):
        """import_pg receives only the target connection, archive, and reporter."""
        mock_import_pg.return_value = _make_import_result()

        # Create a dummy tar.gz file
        archive = tmp_path / "test.tar.gz"
        import tarfile

        with tarfile.open(archive, "w:gz"):
            pass  # empty tarball

        result = runner.invoke(
            migrate_app,
            [
                "import",
                "--db-url",
                "postgresql://u:p@host/db",
                "--archive",
                str(archive),
            ],
        )

        assert result.exit_code == 0, result.output
        mock_import_pg.assert_called_once()
        args, kwargs = mock_import_pg.call_args
        # First arg: PgConnParams
        assert args[0].dsn == "postgresql://u:p@host/db"
        # Second arg: archive path
        assert args[1] == archive
        # Third arg: reporter
        assert hasattr(args[2], "update")
        assert not kwargs

    @patch("observal_cli.cmd_migrate.import_pg", new_callable=AsyncMock)
    def test_import_without_target_identity_flags(self, mock_import_pg, tmp_path):
        """The import command has no target identity options."""
        mock_import_pg.return_value = _make_import_result()

        archive = tmp_path / "test.tar.gz"
        import tarfile

        with tarfile.open(archive, "w:gz"):
            pass

        result = runner.invoke(
            migrate_app,
            ["import", "--db-url", "postgresql://u:p@h/d", "--archive", str(archive)],
        )

        assert result.exit_code == 0, result.output
        _, kwargs = mock_import_pg.call_args
        assert not kwargs


# ── Validate command tests ───────────────────────────────────


class TestValidateCommand:
    """Verify validate_cmd passes correct args to validate_pg."""

    @patch("observal_cli.cmd_migrate.validate_pg", new_callable=AsyncMock)
    def test_validate_without_db_url(self, mock_validate_pg, tmp_path):
        """validate_pg receives None for pg_params when no --db-url is given."""
        mock_validate_pg.return_value = _make_validation_result()

        archive = tmp_path / "test.tar.gz"
        import tarfile

        with tarfile.open(archive, "w:gz"):
            pass

        result = runner.invoke(
            migrate_app,
            ["validate", "--archive", str(archive)],
        )

        assert result.exit_code == 0, result.output
        mock_validate_pg.assert_called_once()
        args = mock_validate_pg.call_args[0]
        # First arg: pg_params (None when no --db-url)
        assert args[0] is None
        # Second arg: archive path
        assert args[1] == archive

    @patch("observal_cli.cmd_migrate.validate_pg", new_callable=AsyncMock)
    def test_validate_with_db_url(self, mock_validate_pg, tmp_path):
        """validate_pg receives PgConnParams when --db-url is given."""
        mock_validate_pg.return_value = _make_validation_result()

        archive = tmp_path / "test.tar.gz"
        import tarfile

        with tarfile.open(archive, "w:gz"):
            pass

        result = runner.invoke(
            migrate_app,
            ["validate", "--archive", str(archive), "--db-url", "postgresql://u:p@h/d"],
        )

        assert result.exit_code == 0, result.output
        args = mock_validate_pg.call_args[0]
        assert args[0].dsn == "postgresql://u:p@h/d"


# ── Export telemetry command tests ───────────────────────────


class TestExportTelemetryCommand:
    """Verify export-telemetry passes correct args to export_telemetry."""

    @patch("observal_cli.cmd_migrate.export_telemetry", new_callable=AsyncMock)
    def test_export_telemetry_passes_store_params(self, mock_export, tmp_path):
        mock_export.return_value = _make_telemetry_export_result()
        output_dir = tmp_path / "out"

        result = runner.invoke(
            migrate_app,
            [
                "export-telemetry",
                "--telemetry-url",
                "http://localhost:8125",
                "--telemetry-token",
                "secret",
                "--output-dir",
                str(output_dir),
                "--migration-id",
                "mig-1",
                "--since",
                "2026-06-01 00:00:00",
            ],
        )

        assert result.exit_code == 0, result.output
        mock_export.assert_called_once()
        args, kwargs = mock_export.call_args
        assert args[0].url == "http://localhost:8125" and args[0].token == "secret"
        assert args[1] == Path(str(output_dir))
        assert hasattr(args[2], "update")
        assert kwargs == {"migration_id": "mig-1", "since": "2026-06-01 00:00:00"}


# ── Import telemetry command tests ───────────────────────────


class TestImportTelemetryCommand:
    """Verify import-telemetry passes correct args to import_telemetry."""

    @patch("observal_cli.cmd_migrate.import_telemetry", new_callable=AsyncMock)
    def test_import_telemetry_passes_store_params(self, mock_import, tmp_path):
        mock_import.return_value = _make_telemetry_import_result()
        input_dir = tmp_path / "telemetry"
        input_dir.mkdir()

        result = runner.invoke(
            migrate_app,
            ["import-telemetry", "--telemetry-url", "http://localhost:8125", "--input-dir", str(input_dir)],
        )

        assert result.exit_code == 0, result.output
        args, kwargs = mock_import.call_args
        assert args[0].url == "http://localhost:8125"
        assert args[1] == input_dir
        assert hasattr(args[2], "update")
        assert kwargs == {"rebuild": True}

    @patch("observal_cli.cmd_migrate.import_telemetry", new_callable=AsyncMock)
    def test_import_telemetry_can_skip_rebuild(self, mock_import, tmp_path):
        mock_import.return_value = _make_telemetry_import_result()
        input_dir = tmp_path / "telemetry"
        input_dir.mkdir()

        result = runner.invoke(
            migrate_app,
            [
                "import-telemetry",
                "--telemetry-url",
                "http://localhost:8125",
                "--input-dir",
                str(input_dir),
                "--no-rebuild",
            ],
        )

        assert result.exit_code == 0, result.output
        assert mock_import.call_args.kwargs == {"rebuild": False}


# ── Validate telemetry command tests ─────────────────────────


class TestValidateTelemetryCommand:
    """Verify validate-telemetry passes correct args to validate_telemetry."""

    @patch("observal_cli.cmd_migrate.validate_telemetry", new_callable=AsyncMock)
    def test_validate_telemetry_with_all_options(self, mock_validate, tmp_path):
        mock_validate.return_value = _make_telemetry_validation_result()
        input_dir = tmp_path / "telemetry"
        input_dir.mkdir()

        result = runner.invoke(
            migrate_app,
            [
                "validate-telemetry",
                "--input-dir",
                str(input_dir),
                "--telemetry-url",
                "http://localhost:8125",
                "--target-db-url",
                "postgresql://u:p@h/d",
            ],
        )

        assert result.exit_code == 0, result.output
        args = mock_validate.call_args[0]
        assert args[0].url == "http://localhost:8125"
        assert args[1].dsn == "postgresql://u:p@h/d"
        assert args[2] == input_dir
        assert hasattr(args[3], "update")

    @patch("observal_cli.cmd_migrate.validate_telemetry", new_callable=AsyncMock)
    def test_validate_telemetry_without_optional_urls(self, mock_validate, tmp_path):
        mock_validate.return_value = _make_telemetry_validation_result()
        input_dir = tmp_path / "telemetry"
        input_dir.mkdir()

        result = runner.invoke(migrate_app, ["validate-telemetry", "--input-dir", str(input_dir)])

        assert result.exit_code == 0, result.output
        args = mock_validate.call_args[0]
        assert args[0] is None
        assert args[1] is None


# ── Cutover command tests ────────────────────────────────────


class TestCutoverCommand:
    @patch("observal_cli.cmd_migrate.run_cutover", new_callable=AsyncMock)
    def test_cutover_passes_both_connections_and_flags(self, mock_cutover, tmp_path):
        from observal_shared.migration import CutoverState

        mock_cutover.return_value = CutoverState(
            migration_id="cutover-1",
            phase="done",
            source_counts={"session_events": 3},
            verification={"target_counts": {"session_events": 3}, "spot_check": {"sampled": 1, "mismatched": []}},
            verified_at="2026-06-01T00:00:00+00:00",
        )

        result = runner.invoke(
            migrate_app,
            [
                "telemetry-cutover",
                "--clickhouse-url",
                "clickhouses://default:pw@ch:8443/observal",
                "--telemetry-url",
                "http://localhost:8125",
                "--artifact-dir",
                str(tmp_path / "cutover"),
                "--resume",
                "--spot-check",
                "5",
            ],
        )

        assert result.exit_code == 0, result.output
        args, kwargs = mock_cutover.call_args
        assert args[0].url == "clickhouses://default:pw@ch:8443/observal"
        assert args[1].url == "http://localhost:8125"
        assert args[2] == tmp_path / "cutover"
        assert kwargs == {"resume": True, "verify_only": False, "spot_check_sessions": 5}
        assert "Cutover complete" in result.output

    @patch("observal_cli.cmd_migrate.reverse_cutover", new_callable=AsyncMock)
    def test_reverse_exports_store_rows(self, mock_reverse, tmp_path):
        mock_reverse.return_value = {"migration_id": "reverse-1", "output_dir": str(tmp_path), "rows": 7}

        result = runner.invoke(
            migrate_app,
            [
                "telemetry-cutover",
                "--clickhouse-url",
                "clickhouses://default:pw@ch:8443/observal",
                "--telemetry-url",
                "http://localhost:8125",
                "--artifact-dir",
                str(tmp_path),
                "--reverse",
                "--since",
                "2026-06-01 00:00:00",
            ],
        )

        assert result.exit_code == 0, result.output
        assert mock_reverse.call_args.kwargs == {"since": "2026-06-01 00:00:00"}
        assert "Reverse export complete" in result.output


# ── Error handling tests ─────────────────────────────────────


class TestErrorHandling:
    """Verify MigrationError is caught and converted to typer.Exit(1)."""

    @patch("observal_cli.cmd_migrate.export_pg", new_callable=AsyncMock)
    def test_migration_error_causes_exit_1(self, mock_export_pg, tmp_path):
        """A MigrationError from the service should result in exit code 1."""
        from observal_shared.migration.exceptions import ConnectionFailedError

        mock_export_pg.side_effect = ConnectionFailedError("Connection refused")
        output = tmp_path / "out.tar.gz"

        result = runner.invoke(
            migrate_app,
            ["export", "--db-url", "postgresql://u:p@h/d", "--file", str(output)],
        )

        assert result.exit_code == 9
        assert "connection failed" in result.output.lower()
