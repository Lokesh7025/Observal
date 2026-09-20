# SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Prometheus metrics for the telemetry store."""

from prometheus_client import Counter, Gauge, Histogram

query_latency = Histogram(
    "telemetry_query_seconds", "Read query latency", buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30)
)
write_latency = Histogram(
    "telemetry_write_seconds", "Write transaction latency", buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 60)
)
query_timeouts_total = Counter("telemetry_query_timeouts_total", "Queries interrupted on timeout")
busy_total = Counter("telemetry_busy_total", "Reads rejected because the queue was saturated")
checkpoints_total = Counter("telemetry_checkpoints_total", "CHECKPOINT statements executed")
table_rows = Gauge("telemetry_table_rows", "Rows per table (sampled on /v1/stats)", ["table"])
file_bytes = Gauge("telemetry_file_bytes", "Database file size in bytes")
