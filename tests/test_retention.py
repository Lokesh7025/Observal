# SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Retention purge against a real telemetry store (acceptance A-12)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from observal_shared.migration.constants import DEFAULT_PROJECT_ID
from services import retention

UID = "11111111-1111-1111-1111-111111111111"


async def _seed(telemetry, session_id: str, ts: str, n: int = 3, raw: str = '{"x":1}'):
    rows = [
        {
            "session_id": session_id,
            "project_id": DEFAULT_PROJECT_ID,
            "user_id": UID,
            "harness": "claude-code",
            "line_offset": i,
            "line_hash": f"h{i}",
            "event_type": "user_prompt",
            "timestamp": ts,
            "raw_line": raw,
            "content_preview": "p",
        }
        for i in range(n)
    ]
    r = await telemetry.post(
        "/v1/write/session-batch",
        json={
            "events": rows,
            "advance_checkpoint": {
                "project_id": DEFAULT_PROJECT_ID,
                "user_id": UID,
                "harness": "claude-code",
                "session_id": session_id,
            },
        },
    )
    assert r.status_code == 200, r.text


async def _count(telemetry, sql: str) -> int:
    r = await telemetry.post("/v1/query", json={"sql": sql})
    assert r.status_code == 200, r.text
    return int(next(iter(r.json()["rows"][0].values())))


async def test_time_based_purge_deletes_old_events_and_orphan_summaries(telemetry):
    await _seed(telemetry, "old", "2024-01-01 00:00:00.000")
    await _seed(telemetry, "new", "2026-05-01 00:00:00.000")
    cutoff = "2025-01-01 00:00:00.000"

    stats = await retention._purge_time_based(DEFAULT_PROJECT_ID, cutoff, retention.TIME_PURGE_TABLES)
    assert stats == {"session_events": 3}
    orphans = await retention._purge_session_stats_orphans(DEFAULT_PROJECT_ID)
    assert orphans == 2  # summary + checkpoint for "old"

    assert await _count(telemetry, "SELECT count(*) FROM session_events") == 3
    assert await _count(telemetry, "SELECT count(*) FROM session_stats_agg") == 1
    assert await _count(telemetry, "SELECT count(*) FROM session_checkpoints") == 1


async def test_has_data(telemetry):
    assert await retention._has_data(DEFAULT_PROJECT_ID) is False
    await _seed(telemetry, "s", "2026-05-01 00:00:00.000", n=1)
    assert await retention._has_data(DEFAULT_PROJECT_ID) is True
    assert await retention._has_data("other-project") is False


async def test_count_based_purge_deletes_oldest_days_over_limit(telemetry):
    now = datetime.now(UTC)
    for day in range(5):
        ts = (now - timedelta(days=day)).strftime("%Y-%m-%d 12:00:00.000")
        await _seed(telemetry, f"s{day}", ts, n=1)

    assert await retention._purge_count_based(DEFAULT_PROJECT_ID, max_trace_count=10) == 0
    assert await _count(telemetry, "SELECT count(*) FROM session_events") == 5

    # The day on which the running total crosses the limit is kept; older days go.
    assert await retention._purge_count_based(DEFAULT_PROJECT_ID, max_trace_count=2) == 1
    remaining = await _count(telemetry, "SELECT count(DISTINCT session_id) FROM session_events")
    assert remaining == 3
    assert await _count(telemetry, "SELECT count(*) FROM session_stats_agg") == 3


async def test_expire_raw_lines_and_audit_purge(telemetry):
    await _seed(telemetry, "old", "2024-01-01 00:00:00.000")
    await _seed(telemetry, "new", datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.000"))
    expired = await retention.expire_raw_lines()
    assert expired == 3
    r = await telemetry.post(
        "/v1/query",
        json={"sql": "SELECT raw_line, raw_line_truncated FROM session_events WHERE session_id = 'old' LIMIT 1"},
    )
    assert r.json()["rows"] == [{"raw_line": "", "raw_line_truncated": 2}]
    assert await _count(telemetry, "SELECT count(*) FROM session_events WHERE session_id='new' AND raw_line <> ''") == 3

    await telemetry.post(
        "/v1/write/append",
        json={
            "table": "audit_log",
            "rows": [
                {"event_id": "11111111-1111-1111-1111-111111111111", "timestamp": "2020-01-01 00:00:00", "action": "a"},
                {"event_id": "22222222-2222-2222-2222-222222222222", "timestamp": "2026-05-01 00:00:00", "action": "b"},
            ],
        },
    )
    stats = await retention.purge_audit_tables()
    assert stats == {"audit_log": 1, "security_events": 0}


async def test_store_outage_is_reported_not_swallowed(telemetry):
    import httpx

    from services.telemetry import client as tclient

    class _Down(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise httpx.ConnectError("down")

    tclient.set_client(httpx.AsyncClient(transport=_Down(), base_url="http://telemetry"))
    assert await retention._delete_batch("session_events", "timestamp", DEFAULT_PROJECT_ID, "2025-01-01") == 0
    assert await retention._has_data(DEFAULT_PROJECT_ID) is False
    assert await retention.expire_raw_lines() == 0


async def test_run_retention_purge_uses_default_project_and_settings(telemetry):
    await _seed(telemetry, "old", "2024-01-01 00:00:00.000")
    await _seed(telemetry, "new", datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.000"))

    async def get_bool(key, default=False):
        return key == "retention.enabled"

    async def get_int(key, default=0):
        return {"retention.trace_days": 30}.get(key, default)

    with (
        patch.object(retention.ds, "get_bool", new=get_bool),
        patch.object(retention.ds, "get_int", new=get_int),
        patch.object(retention, "_has_inflight_insights", new=AsyncMock(return_value=False)),
        patch.object(retention, "_purge_insight_reports", new=AsyncMock(return_value=0)) as reports,
    ):
        await retention.run_retention_purge()

    reports.assert_awaited_once()
    assert await _count(telemetry, "SELECT count(*) FROM session_events") == 3
    assert await _count(telemetry, "SELECT count(*) FROM session_stats_agg") == 1


@pytest.mark.asyncio
async def test_run_retention_purge_skips_when_disabled():
    with (
        patch.object(retention.ds, "get_bool", new=AsyncMock(return_value=False)),
        patch.object(retention, "_has_data", new=AsyncMock()) as has_data,
    ):
        await retention.run_retention_purge()
    has_data.assert_not_awaited()
