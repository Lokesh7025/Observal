# SPDX-FileCopyrightText: 2026 Hari Srinivasan <harisrini21@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Maintenance background jobs: telemetry store upkeep, component source sync, retention."""

from loguru import logger as optic


async def sync_component_sources(ctx: dict):
    """Background job: sync component sources that are due for re-sync."""
    optic.debug("sync_component_sources")
    from datetime import UTC, datetime

    from sqlalchemy import or_, select

    from database import async_session
    from models.component_source import ComponentSource
    from services.git_mirror_service import sync_source

    async with async_session() as db:
        # Find sources due for sync
        now = datetime.now(UTC)
        stmt = select(ComponentSource).where(
            ComponentSource.auto_sync_interval.isnot(None),
            or_(
                ComponentSource.last_synced_at.is_(None),
                ComponentSource.last_synced_at + ComponentSource.auto_sync_interval < now,
            ),
        )
        result = await db.execute(stmt)
        sources = result.scalars().all()

        for source in sources:
            optic.info("Syncing component source {} ({})", source.id, source.url)
            source.sync_status = "syncing"
            await db.commit()

            sync_result = sync_source(source.url, source.component_type)

            source.last_synced_at = now
            source.sync_status = "success" if sync_result.success else "failed"
            source.sync_error = sync_result.error if not sync_result.success else None
            await db.commit()
            optic.info(
                "Sync {}: {} ({} components)",
                source.url,
                source.sync_status,
                len(sync_result.components),
            )


async def purge_inbox_items(ctx: dict):
    """Delete resolved inbox items past the retention horizon.

    Only ``done`` and ``dismissed`` items are eligible. An ``open`` item is
    unactioned work, and deleting work silently is the exact failure the inbox
    exists to prevent, so it is never purged on age. History rows go with the
    item through ON DELETE CASCADE.
    """
    optic.debug("purge_inbox_items")
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import delete, func, select

    import services.dynamic_settings as ds
    from database import async_session
    from models.inbox import InboxItem, InboxState

    retention_days = await ds.get_int("inbox.retention_days", 90)
    if retention_days <= 0:
        optic.info("inbox retention disabled (inbox.retention_days={})", retention_days)
        return

    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    # Eligibility is re-stated in the DELETE, not just used to gather ids. A user
    # can reopen an item between the two statements, and deleting by id alone
    # would destroy work they just pulled back into their queue along with its
    # history. The predicates make the delete self-guarding.
    eligible = (
        InboxItem.state.in_([InboxState.done, InboxState.dismissed]),
        InboxItem.resolved_at.is_not(None),
        InboxItem.resolved_at < cutoff,
    )
    async with async_session() as db:
        pending = (await db.execute(select(func.count(InboxItem.id)).where(*eligible))).scalar() or 0
        if not pending:
            optic.debug("inbox retention: nothing older than {} days", retention_days)
            return
        result = await db.execute(delete(InboxItem).where(*eligible))
        await db.commit()
        # Report what the delete actually removed, which can be fewer than the
        # count above if someone reopened an item in between.
        optic.info(
            "inbox retention: purged {} resolved item(s) older than {} days",
            result.rowcount,
            retention_days,
        )


async def maintain_telemetry(ctx: dict):
    """Periodic telemetry-store maintenance.

    Runs a CHECKPOINT so freed blocks are reclaimed, expires ``raw_line`` bodies
    past their 30-day window, applies the 730-day audit retention, and logs table
    sizes so growth is visible in dev logs before it becomes a problem.
    """
    del ctx
    optic.debug("maintain_telemetry")
    from services.retention import expire_raw_lines, purge_audit_tables
    from services.telemetry import TelemetryError, checkpoint, health, table_counts

    expired = await expire_raw_lines()
    audit_stats = await purge_audit_tables()
    try:
        await checkpoint()
    except TelemetryError as e:
        optic.warning("telemetry checkpoint failed: {}", e)

    try:
        counts = await table_counts()
    except TelemetryError as e:
        optic.warning("telemetry table count failed: {}", e)
        counts = {}
    status = await health() or {}
    optic.info(
        "telemetry maintenance: expired_raw_lines={} audit_purged={} file_bytes={} wal_bytes={} rows={}",
        expired,
        audit_stats,
        status.get("file_bytes"),
        status.get("wal_bytes"),
        {k: v for k, v in counts.items() if not k.startswith("schema_")},
    )
