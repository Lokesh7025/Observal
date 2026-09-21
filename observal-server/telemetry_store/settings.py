# SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Telemetry service settings (environment-driven, boot-time only)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def _read_secret(name: str) -> str:
    """Read ``NAME`` or the file pointed to by ``NAME_FILE``."""
    file_path = os.environ.get(f"{name}_FILE")
    if file_path:
        return Path(file_path).read_text().strip()
    return os.environ.get(name, "").strip()


@dataclass(frozen=True)
class TelemetrySettings:
    db_path: Path
    temp_dir: Path
    export_dir: Path
    token: str
    memory_limit: str
    threads: int
    read_threads: int
    read_queue_max: int
    query_timeout_ms: int
    write_queue_timeout_ms: int
    max_result_rows: int
    checkpoint_interval_s: int
    checkpoint_threshold: str
    bind_host: str
    bind_port: int
    metrics_public: bool
    #: When true, the service may run with an empty token (tests / embedded loopback only).
    allow_anonymous: bool
    backup_dir: Path | None = None
    max_import_chunk_bytes: int = 1024 * 1024 * 1024
    min_free_space_bytes: int = 256 * 1024 * 1024

    @classmethod
    def from_env(cls) -> TelemetrySettings:
        db_path = Path(os.environ.get("TELEMETRY_DB_PATH", "/data/telemetry/observal.duckdb"))
        temp_dir = Path(os.environ.get("TELEMETRY_TEMP_DIR", str(db_path.parent / "tmp")))
        export_dir = Path(os.environ.get("TELEMETRY_EXPORT_DIR", str(db_path.parent / "exports")))
        bind = os.environ.get("TELEMETRY_BIND", "0.0.0.0:8125")
        host, _, port = bind.rpartition(":")
        return cls(
            db_path=db_path,
            temp_dir=temp_dir,
            export_dir=export_dir,
            token=_read_secret("TELEMETRY_TOKEN"),
            memory_limit=os.environ.get("TELEMETRY_MEMORY_LIMIT", "1536MB"),
            threads=_env_int("TELEMETRY_THREADS", 4),
            read_threads=_env_int("TELEMETRY_READ_THREADS", 4),
            read_queue_max=_env_int("TELEMETRY_READ_QUEUE_MAX", 64),
            query_timeout_ms=_env_int("TELEMETRY_QUERY_TIMEOUT_MS", 30_000),
            write_queue_timeout_ms=_env_int("TELEMETRY_WRITE_QUEUE_TIMEOUT_MS", 10_000),
            max_result_rows=_env_int("TELEMETRY_MAX_RESULT_ROWS", 200_000),
            checkpoint_interval_s=_env_int("TELEMETRY_CHECKPOINT_INTERVAL_S", 300),
            checkpoint_threshold=os.environ.get("TELEMETRY_CHECKPOINT_THRESHOLD", "256MB"),
            bind_host=host or "0.0.0.0",
            bind_port=int(port or 8125),
            metrics_public=os.environ.get("TELEMETRY_METRICS_PUBLIC", "false").lower() in {"1", "true", "yes"},
            allow_anonymous=os.environ.get("TELEMETRY_ALLOW_ANONYMOUS", "false").lower() in {"1", "true", "yes"},
            backup_dir=Path(os.environ.get("TELEMETRY_BACKUP_DIR", str(db_path.parent / "backups"))),
            max_import_chunk_bytes=_env_int("TELEMETRY_MAX_IMPORT_CHUNK_BYTES", 1024 * 1024 * 1024),
            min_free_space_bytes=_env_int("TELEMETRY_MIN_FREE_SPACE_BYTES", 256 * 1024 * 1024),
        )

    def validate(self) -> None:
        if not self.token and not self.allow_anonymous:
            raise ValueError("TELEMETRY_TOKEN (or TELEMETRY_TOKEN_FILE) is required")
        if self.read_threads < 1 or self.threads < 1:
            raise ValueError("TELEMETRY_THREADS and TELEMETRY_READ_THREADS must be >= 1")
        if self.query_timeout_ms < 100:
            raise ValueError("TELEMETRY_QUERY_TIMEOUT_MS must be >= 100")
        if self.max_import_chunk_bytes < 1:
            raise ValueError("TELEMETRY_MAX_IMPORT_CHUNK_BYTES must be >= 1")
        if self.min_free_space_bytes < 0:
            raise ValueError("TELEMETRY_MIN_FREE_SPACE_BYTES must be >= 0")
