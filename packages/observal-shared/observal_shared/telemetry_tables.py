# SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Registry of DuckDB telemetry tables.

Shared by the telemetry service (write semantics), the API client (table
allow-list), and the migration importer (column mapping). Logical keys are
enforced by the single writer, never by DuckDB constraints.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

WriteMode = Literal["replace", "append"]


@dataclass(frozen=True)
class TelemetryTable:
    name: str
    mode: WriteMode
    key_columns: tuple[str, ...]
    time_column: str
    columns: tuple[str, ...]
    #: Column used to decide whether an imported row may overwrite a live row.
    version_column: str | None = None
    #: Columns computed by the importer/client, absent from ClickHouse exports.
    derived_columns: tuple[str, ...] = field(default_factory=tuple)


SESSION_EVENTS = TelemetryTable(
    name="session_events",
    mode="replace",
    key_columns=("session_key", "line_offset"),
    time_column="timestamp",
    version_column="ingested_at",
    derived_columns=("session_key", "parent_session_key"),
    columns=(
        "session_key",
        "parent_session_key",
        "session_id",
        "project_id",
        "user_id",
        "harness",
        "agent_id",
        "agent_version",
        "layer_hash",
        "line_offset",
        "source_end_offset",
        "line_hash",
        "source_sha256",
        "is_source_record",
        "rendered",
        "event_type",
        "timestamp",
        "uuid",
        "parent_uuid",
        "tool_name",
        "tool_id",
        "content_preview",
        "content_length",
        "raw_line",
        "raw_line_truncated",
        "ingested_at",
        "credits",
        "parent_session_id",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "model",
    ),
)

SESSION_CHECKPOINTS = TelemetryTable(
    name="session_checkpoints",
    mode="replace",
    key_columns=("session_key",),
    time_column="updated_at",
    version_column="checkpoint_version",
    derived_columns=("session_key",),
    columns=(
        "session_key",
        "project_id",
        "user_id",
        "harness",
        "session_id",
        "acknowledged_line",
        "acknowledged_offset",
        "checkpoint_version",
        "updated_at",
    ),
)

SESSION_STATS_AGG = TelemetryTable(
    name="session_stats_agg",
    mode="replace",
    key_columns=("session_key",),
    time_column="first_event_time",
    version_column="summary_version",
    derived_columns=("session_key",),
    columns=(
        "session_key",
        "project_id",
        "session_id",
        "user_id",
        "harness",
        "agent_id",
        "agent_version",
        "parent_session_id",
        "layer_hash",
        "first_event_time",
        "last_event_time",
        "event_count",
        "prompt_count",
        "tool_call_count",
        "tool_result_count",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "total_credits",
        "model",
        "summary_version",
        "updated_at",
    ),
)

LAYER_SNAPSHOTS = TelemetryTable(
    name="layer_snapshots",
    mode="replace",
    key_columns=("snapshot_key",),
    time_column="uploaded_at",
    version_column="uploaded_at",
    derived_columns=("snapshot_key",),
    columns=(
        "snapshot_key",
        "hash",
        "project_id",
        "user_id",
        "harness",
        "content",
        "uploaded_at",
        "file_count",
        "total_size",
        "lockfile_hash",
    ),
)

AUDIT_LOG = TelemetryTable(
    name="audit_log",
    mode="append",
    key_columns=("event_id",),
    time_column="timestamp",
    columns=(
        "event_id",
        "timestamp",
        "actor_id",
        "actor_email",
        "actor_role",
        "action",
        "resource_type",
        "resource_id",
        "resource_name",
        "http_method",
        "http_path",
        "status_code",
        "ip_address",
        "user_agent",
        "detail",
        "sensitivity",
        "request_id",
        "outcome",
        "duration_ms",
        "chain_hash",
        "source",
    ),
)

SECURITY_EVENTS = TelemetryTable(
    name="security_events",
    mode="append",
    key_columns=("event_id",),
    time_column="timestamp",
    columns=(
        "event_id",
        "timestamp",
        "event_type",
        "severity",
        "actor_id",
        "actor_email",
        "actor_role",
        "target_id",
        "target_type",
        "outcome",
        "source_ip",
        "user_agent",
        "detail",
    ),
)

WEBHOOK_DELIVERIES = TelemetryTable(
    name="webhook_deliveries",
    mode="append",
    key_columns=("delivery_id",),
    time_column="timestamp",
    columns=(
        "delivery_id",
        "event_id",
        "alert_rule_id",
        "attempt_number",
        "timestamp",
        "webhook_url",
        "status_code",
        "delivery_status",
        "error",
        "duration_ms",
        "payload_size",
    ),
)

TELEMETRY_TABLES: dict[str, TelemetryTable] = {
    table.name: table
    for table in (
        SESSION_EVENTS,
        SESSION_CHECKPOINTS,
        SESSION_STATS_AGG,
        LAYER_SNAPSHOTS,
        AUDIT_LOG,
        SECURITY_EVENTS,
        WEBHOOK_DELIVERIES,
    )
}

#: Tables imported from a ClickHouse export. Derived tables are rebuilt instead.
IMPORTED_TABLES: tuple[str, ...] = (
    "session_events",
    "layer_snapshots",
    "audit_log",
    "security_events",
    "webhook_deliveries",
)

#: Tables rebuilt from ``session_events`` after an import.
DERIVED_TABLES: tuple[str, ...] = ("session_stats_agg", "session_checkpoints")

#: Tables deleted by the danger-zone purge and by project-scoped deletes.
PROJECT_SCOPED_TABLES: tuple[str, ...] = ("session_events", "session_stats_agg", "session_checkpoints")

#: Sentinel ``line_offset`` for non-source extra rows (e.g. ``kiro_credits``).
EXTRA_ROW_LINE_OFFSET = 0xFFFFFFFF


def get_table(name: str) -> TelemetryTable:
    try:
        return TELEMETRY_TABLES[name]
    except KeyError as exc:
        raise KeyError(f"unknown telemetry table: {name}") from exc


__all__ = [
    "AUDIT_LOG",
    "DERIVED_TABLES",
    "EXTRA_ROW_LINE_OFFSET",
    "IMPORTED_TABLES",
    "LAYER_SNAPSHOTS",
    "PROJECT_SCOPED_TABLES",
    "SECURITY_EVENTS",
    "SESSION_CHECKPOINTS",
    "SESSION_EVENTS",
    "SESSION_STATS_AGG",
    "TELEMETRY_TABLES",
    "WEBHOOK_DELIVERIES",
    "TelemetryTable",
    "WriteMode",
    "get_table",
]
