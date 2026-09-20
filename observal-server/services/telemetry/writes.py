# SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Append/replace writers for non-session telemetry tables."""

from __future__ import annotations

from typing import Any

from loguru import logger as optic

from services.telemetry import client
from services.telemetry.sql import normalize_ts, now_ms


async def insert_audit_log(events: list[dict]) -> None:
    """Batch insert audit log events. Failures are logged at error level with the row count."""
    if not events:
        return
    rows = []
    for e in events:
        rows.append(
            {
                "event_id": e["event_id"],
                "timestamp": normalize_ts(e.get("timestamp")) or now_ms(),
                "actor_id": e.get("actor_id", ""),
                "actor_email": e.get("actor_email", ""),
                "actor_role": e.get("actor_role", ""),
                "action": e.get("action", ""),
                "resource_type": e.get("resource_type", ""),
                "resource_id": e.get("resource_id", ""),
                "resource_name": e.get("resource_name", ""),
                "http_method": e.get("http_method", ""),
                "http_path": e.get("http_path", ""),
                "status_code": e.get("status_code", 0),
                "ip_address": e.get("ip_address", ""),
                "user_agent": e.get("user_agent", ""),
                "detail": e.get("detail", ""),
                "sensitivity": e.get("sensitivity", "standard"),
                "request_id": e.get("request_id", ""),
                "outcome": e.get("outcome", ""),
                "duration_ms": e.get("duration_ms", 0.0),
                "chain_hash": e.get("chain_hash", ""),
                "source": e.get("source", "server"),
            }
        )
    try:
        await client.write("/v1/write/append", {"table": "audit_log", "rows": rows})
    except client.TelemetryError as exc:
        optic.error("failed to insert {} audit events - audit trail has a gap: {}", len(rows), exc)


async def insert_security_event(row: dict[str, Any]) -> None:
    row = dict(row)
    if "timestamp" in row:
        row["timestamp"] = normalize_ts(str(row["timestamp"]))
    try:
        await client.write("/v1/write/append", {"table": "security_events", "rows": [row]})
    except client.TelemetryError as exc:
        optic.error("failed to record security event {}: {}", row.get("event_type"), exc)


async def insert_webhook_deliveries(records: list[dict]) -> None:
    if not records:
        return
    rows = []
    for r in records:
        rows.append(
            {
                "delivery_id": r["delivery_id"],
                "event_id": r["event_id"],
                "alert_rule_id": r["alert_rule_id"],
                "attempt_number": r["attempt_number"],
                "timestamp": normalize_ts(r["timestamp"]),
                "webhook_url": r["webhook_url"],
                "status_code": r["status_code"],
                "delivery_status": r["delivery_status"],
                "error": r.get("error"),
                "duration_ms": r["duration_ms"],
                "payload_size": r["payload_size"],
            }
        )
    try:
        await client.write("/v1/write/append", {"table": "webhook_deliveries", "rows": rows})
    except client.TelemetryError as exc:
        optic.error("failed to record {} webhook deliveries: {}", len(rows), exc)


async def insert_layer_snapshot(row: dict) -> None:
    """Replace one layer snapshot by ``(project_id, user_id, hash)``. Raises on failure."""
    await client.write("/v1/write/replace", {"table": "layer_snapshots", "rows": [row]})


async def delete_rows(table: str, where: dict[str, Any]) -> int:
    body = await client.write("/v1/write/delete", {"table": table, "where": where})
    return int(body.get("rows_deleted", 0))


async def delete_orphan_summaries(project_id: str) -> int:
    body = await client.write("/v1/write/delete-orphan-summaries", {"project_id": project_id})
    return int(body.get("rows_deleted", 0))


async def expire_raw_lines(before: str) -> int:
    body = await client.write("/v1/write/expire-raw-lines", {"before": before})
    return int(body.get("rows_expired", 0))


async def checkpoint() -> None:
    await client.write("/v1/admin/checkpoint", {})
