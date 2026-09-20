# SPDX-FileCopyrightText: 2026 Hari Srinivasan <harisrini21@gmail.com>
# SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Deployment-wide data retention purge service."""

from datetime import UTC, datetime, timedelta

from loguru import logger as optic
from sqlalchemy import delete, select

import services.dynamic_settings as ds
from database import async_session
from models.insight_report import InsightReport, InsightReportStatus
from observal_shared.migration.constants import DEFAULT_PROJECT_ID
from services.telemetry import TelemetryError, delete_orphan_summaries, delete_rows, query_one

TIME_PURGE_TABLES = {"session_events": "timestamp"}

#: ClickHouse kept ``raw_line`` for 30 days via a column TTL; the store expires it nightly.
RAW_LINE_RETENTION_DAYS = 30
#: Audit and security events were kept for 730 days via table TTL.
AUDIT_RETENTION_DAYS = 730


async def _delete_batch(table: str, time_col: str, project_id: str, cutoff_str: str) -> int:
    """Delete rows older than *cutoff_str* for one table. Returns rows deleted."""
    del time_col  # the store resolves the time column from the table registry
    try:
        return await delete_rows(table, {"project_id": project_id, "timestamp_lt": cutoff_str})
    except TelemetryError as exc:
        optic.error("retention delete failed on table {}: {}", table, exc)
        return 0


async def _has_data(project_id: str) -> bool:
    try:
        row = await query_one(
            "SELECT 1 AS present FROM session_events WHERE project_id = $pid LIMIT 1", {"pid": project_id}
        )
    except TelemetryError as exc:
        optic.error("retention: could not check for data: {}", exc)
        return False
    return bool(row)


async def _has_inflight_insights() -> bool:
    async with async_session() as db:
        report_id = (
            await db.execute(
                select(InsightReport.id)
                .where(InsightReport.status.in_([InsightReportStatus.pending, InsightReportStatus.running]))
                .limit(1)
            )
        ).scalar_one_or_none()
    return report_id is not None


async def _purge_time_based(project_id: str, cutoff_str: str, tables: dict[str, str]) -> dict[str, int]:
    stats: dict[str, int] = {}
    for table, time_col in tables.items():
        stats[table] = await _delete_batch(table, time_col, project_id, cutoff_str)
    return stats


async def _purge_session_stats_orphans(project_id: str) -> int:
    try:
        return await delete_orphan_summaries(project_id)
    except TelemetryError as exc:
        optic.error("retention: orphan summary cleanup failed: {}", exc)
        return 0


async def _purge_insight_reports(score_cutoff: datetime) -> int:
    async with async_session() as db:
        completed = await db.execute(
            delete(InsightReport).where(
                InsightReport.completed_at < score_cutoff,
                InsightReport.status == InsightReportStatus.completed,
            )
        )
        stuck = await db.execute(
            delete(InsightReport).where(
                InsightReport.created_at < score_cutoff,
                InsightReport.status.in_([InsightReportStatus.failed, InsightReportStatus.pending]),
            )
        )
        await db.commit()
    return int(completed.rowcount or 0) + int(stuck.rowcount or 0)


async def _purge_count_based(project_id: str, max_trace_count: int) -> int:
    from services.telemetry import tq

    try:
        data = await tq(
            'SELECT CAST("timestamp" AS DATE) AS day, count(DISTINCT session_id) AS cnt '
            "FROM session_events WHERE project_id = $pid "
            'AND "timestamp" >= now()::TIMESTAMP - to_days(730) '
            "GROUP BY day ORDER BY day DESC LIMIT 730",
            {"pid": project_id},
        )
    except TelemetryError as exc:
        optic.error("retention: count-based purge scan failed: {}", exc)
        return 0
    running_total = 0
    cutoff_day = None
    for row in data:
        running_total += int(row["cnt"])
        if running_total > max_trace_count:
            cutoff_day = row["day"]
            break
    if cutoff_day is None:
        return 0

    await _delete_batch("session_events", "timestamp", project_id, f"{cutoff_day} 00:00:00.000")
    await _purge_session_stats_orphans(project_id)
    return 1


async def expire_raw_lines(now: datetime | None = None) -> int:
    """Drop transcript bodies older than the raw-line window (ClickHouse column TTL equivalent)."""
    from services.telemetry import expire_raw_lines as _expire

    now = now or datetime.now(UTC)
    before = (now - timedelta(days=RAW_LINE_RETENTION_DAYS)).strftime("%Y-%m-%d %H:%M:%S.000")
    try:
        expired = await _expire(before)
    except TelemetryError as exc:
        optic.error("raw_line expiry failed: {}", exc)
        return 0
    if expired:
        optic.info("expired raw_line on {} session events older than {} days", expired, RAW_LINE_RETENTION_DAYS)
    return expired


async def purge_audit_tables(now: datetime | None = None) -> dict[str, int]:
    """Apply the 730-day retention to audit and security events."""
    now = now or datetime.now(UTC)
    cutoff = (now - timedelta(days=AUDIT_RETENTION_DAYS)).strftime("%Y-%m-%d %H:%M:%S.000")
    stats: dict[str, int] = {}
    for table in ("audit_log", "security_events"):
        try:
            stats[table] = await delete_rows(table, {"timestamp_lt": cutoff})
        except TelemetryError as exc:
            optic.error("audit retention delete failed on {}: {}", table, exc)
            stats[table] = 0
    return stats


async def run_retention_purge(ctx: dict | None = None):
    """Run the configured retention policy for the deployment."""
    del ctx
    if not await ds.get_bool("retention.enabled"):
        return

    trace_days = await ds.get_int("retention.trace_days", default=0)
    score_days = await ds.get_int("retention.score_days", default=0)
    max_trace_count = await ds.get_int("retention.max_trace_count", default=0)
    if not trace_days and not score_days and not max_trace_count:
        return
    if await _has_data(DEFAULT_PROJECT_ID) is False:
        return
    if await _has_inflight_insights():
        optic.info("skipping retention purge while insights are in flight")
        return

    now = datetime.now(UTC)
    stats: dict[str, object] = {}
    if trace_days:
        cutoff = (now - timedelta(days=trace_days)).strftime("%Y-%m-%d %H:%M:%S.000")
        stats["time"] = await _purge_time_based(DEFAULT_PROJECT_ID, cutoff, TIME_PURGE_TABLES)
        stats["orphans"] = await _purge_session_stats_orphans(DEFAULT_PROJECT_ID)

    score_days = score_days or (trace_days * 2 if trace_days else 0)
    if score_days:
        stats["insight_reports"] = await _purge_insight_reports(now - timedelta(days=max(score_days, 30)))
    if max_trace_count:
        stats["count_purge"] = await _purge_count_based(DEFAULT_PROJECT_ID, max_trace_count)

    optic.info("retention purge complete: {}", stats)


__all__ = [
    "AUDIT_RETENTION_DAYS",
    "RAW_LINE_RETENTION_DAYS",
    "TIME_PURGE_TABLES",
    "expire_raw_lines",
    "purge_audit_tables",
    "run_retention_purge",
]
