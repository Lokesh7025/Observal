# SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Telemetry store client package (DuckDB service over HTTP).

``tq`` is the single read entry point; everything raises ``TelemetryError`` on
failure so routes never return empty results for an outage.
"""

from services.telemetry.client import (
    TelemetryBusyError,
    TelemetryError,
    TelemetryQueryError,
    TelemetryResultTooLargeError,
    TelemetryTimeoutError,
    TelemetryUnavailableError,
    close_client,
    get_client,
    health,
    query,
    query_one,
    scalar,
    set_client,
    telemetry_health,
    verify_telemetry,
    write,
)
from services.telemetry.events import (
    insert_session_batch,
    insert_session_checkpoint,
    insert_session_events,
    query_existing_for_dedup,
    query_session_checkpoint,
    query_session_source_manifest,
    query_source_records_after,
    refresh_session_summary,
)
from services.telemetry.reads import query_recent_events, table_counts
from services.telemetry.sql import FAR_FUTURE, VALID_TS, in_condition, interval_ago, normalize_ts, now_ms, now_ts
from services.telemetry.writes import (
    checkpoint,
    delete_orphan_summaries,
    delete_rows,
    expire_raw_lines,
    insert_audit_log,
    insert_layer_snapshot,
    insert_security_event,
    insert_webhook_deliveries,
)

tq = query

__all__ = [
    "FAR_FUTURE",
    "VALID_TS",
    "TelemetryBusyError",
    "TelemetryError",
    "TelemetryQueryError",
    "TelemetryResultTooLargeError",
    "TelemetryTimeoutError",
    "TelemetryUnavailableError",
    "checkpoint",
    "close_client",
    "delete_orphan_summaries",
    "delete_rows",
    "expire_raw_lines",
    "get_client",
    "health",
    "in_condition",
    "insert_audit_log",
    "insert_layer_snapshot",
    "insert_security_event",
    "insert_session_batch",
    "insert_session_checkpoint",
    "insert_session_events",
    "insert_webhook_deliveries",
    "interval_ago",
    "normalize_ts",
    "now_ms",
    "now_ts",
    "query",
    "query_existing_for_dedup",
    "query_one",
    "query_recent_events",
    "query_session_checkpoint",
    "query_session_source_manifest",
    "query_source_records_after",
    "refresh_session_summary",
    "scalar",
    "set_client",
    "table_counts",
    "telemetry_health",
    "tq",
    "verify_telemetry",
    "write",
]
