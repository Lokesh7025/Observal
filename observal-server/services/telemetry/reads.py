# SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Shared aggregate readers."""

from __future__ import annotations

from services.telemetry import client
from services.telemetry.sql import interval_ago


async def query_recent_events(minutes: int = 60) -> dict:
    """Recent session activity counts from session summaries."""
    row = await client.query_one(
        "SELECT coalesce(sum(tool_call_count), 0) AS tools, count(*) AS sessions "
        f"FROM session_stats_agg WHERE last_event_time > {interval_ago('minute', 'minutes')}",
        {"minutes": int(minutes)},
    )
    return {
        "tool_call_events": int(row.get("tools") or 0),
        "agent_interaction_events": int(row.get("sessions") or 0),
    }


async def table_counts() -> dict[str, int]:
    body = await client.get("/v1/stats")
    return {k: int(v) for k, v in body.get("tables", {}).items()}
