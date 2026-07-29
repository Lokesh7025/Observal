# SPDX-FileCopyrightText: 2026 Hari Srinivasan <harisrini21@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Insights background jobs."""

from loguru import logger as optic


async def generate_insight_report(ctx: dict, report_id: str):
    """Background job: generate an insight report for an agent."""
    optic.info("insight_report_started", report_id=report_id)
    try:
        from services.insights import run_single_report

        await run_single_report(report_id)
    except Exception as e:
        optic.error("insight_report_job_failed", report_id=report_id, error=str(e))


async def batch_generate_insights(ctx: dict):
    """Cron job: discover agents needing reports and queue generation."""
    optic.debug("batch_generate_insights")
    try:
        from services.insights import discover_and_queue_reports

        queued = await discover_and_queue_reports()
        if queued > 0:
            optic.info("insight_batch_queued_reports", count=queued)
    except Exception as e:
        optic.error("insight_batch_failed", error=str(e))


async def refresh_user_profiles(ctx: dict):
    """Cron job: refresh every active user's work profile.

    Best-effort warm-up only. Correctness never depends on this job: a stale
    or missing profile is rebuilt lazily on first request. It exists so the
    registry home page rarely pays for a ClickHouse scan.

    The sweep is sequential and unfiltered because there is no cheap
    "recently active" signal in Postgres. A user with no sessions costs one
    indexed ClickHouse lookup that returns nothing, so the floor is low; if
    the job ever outgrows its timeout it is truncated harmlessly, and the
    users it missed simply build on demand.
    """
    optic.debug("refresh_user_profiles")
    try:
        from sqlalchemy import select

        from api.deps import get_project_id
        from database import async_session
        from models.user import User
        from services.user_profile import get_or_build_profile

        refreshed = 0
        async with async_session() as db:
            users = (await db.execute(select(User).where(User.auth_provider != "deactivated"))).scalars().all()
            for user in users:
                try:
                    await get_or_build_profile(db, user.id, get_project_id(user), force=True)
                    refreshed += 1
                except Exception as e:
                    # One bad user must not stop the sweep. The session is
                    # shared across the loop, so a database error leaves it in
                    # an aborted transaction: without this rollback every
                    # later user fails too, and one fault is misreported as N.
                    await db.rollback()
                    optic.warning("user_profile_refresh_failed", user_id=str(user.id), error=str(e))
        optic.info("user_profiles_refreshed", count=refreshed)
    except Exception as e:
        optic.error("user_profile_batch_failed", error=str(e))
