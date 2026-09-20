# SPDX-FileCopyrightText: 2026 Subramania Raja <dhanpraja231@gmail.com>
# SPDX-FileCopyrightText: 2026 Hari Srinivasan <harisrini21@gmail.com>
# SPDX-FileCopyrightText: 2026 Kaushik Kumar <kaushikrjpm10@gmail.com>
# SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com>
# SPDX-FileCopyrightText: 2026 Shaan Narendran <shaannaren06@gmail.com>
# SPDX-FileCopyrightText: 2026 Shreem Seth <shreemseth26@gmail.com>
# SPDX-FileCopyrightText: 2026 SrihariLegend <sriharilegend23@gmail.com>
# SPDX-FileCopyrightText: 2026 Vishnu Muthiah <vishnu.muthiah04@gmail.com>
# SPDX-FileCopyrightText: 2026 tsitu0 <tomsitu0102@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Session listing and detail endpoints - backed by the telemetry store.

Reads ``session_stats_agg`` and ``session_events`` populated by the
/api/v1/ingest/session endpoint.  Uses session parsers to transform
raw JSONL rows into frontend-friendly event dicts.
"""

import asyncio
import uuid as _uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi_cache.decorator import cache
from loguru import logger as optic
from sqlalchemy import select

import services.dynamic_settings as ds
from api.deps import require_role
from database import async_session
from models.user import User, UserRole
from services.telemetry import FAR_FUTURE, in_condition, interval_ago, tq
from services.user_search import resolve_user_filter_values

router = APIRouter(prefix="/api/v1/sessions", tags=["sessions"])


def _is_admin_user(user: User) -> bool:
    optic.trace("user_id={}", user.id)
    return user.role in (UserRole.admin, UserRole.super_admin)


def _has_admin_trace_access(user: User) -> bool:
    """Check if user has admin-level trace access."""
    optic.trace("user_id={}", user.id)
    if not _is_admin_user(user):
        return False
    if user.role == UserRole.super_admin:
        return True
    return not getattr(user, "_trace_privacy", False)


@router.get("")
async def list_sessions(
    status: str | None = Query(None),
    platform: str | None = Query(None),
    user: str | None = Query(None, description="Filter by user name, username, or email"),
    days: int | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    mine: bool = Query(False),
    current_user: User = Depends(require_role(UserRole.user)),
):
    optic.trace("status={}, platform={}, user={}", status, platform, user)
    is_admin = _has_admin_trace_access(current_user)
    uid_str = str(current_user.id)
    capped_days = min(days, 365) if days is not None and days > 0 else days

    optic.debug(
        "list_sessions: user={}, is_admin={}, platform={}, days={}, mine={}",
        uid_str,
        is_admin,
        platform,
        capped_days,
        mine,
    )

    user_ids: list[str] | None = None
    if user:
        async with async_session() as db:
            values = await resolve_user_filter_values(db, user)
        user_ids = values.ids
        if not user_ids:
            return []

    rows = await _list_sessions_query(
        platform=platform,
        user_ids=user_ids,
        days=capped_days,
        is_admin=is_admin,
        uid=uid_str,
        limit=limit,
        offset=offset,
        mine=mine,
    )

    # Resolve user display names from PostgreSQL
    uid_to_name: dict[str, str] = {}
    unresolved_ids: set[str] = set()
    for row in rows:
        uid = row.get("user_id", "")
        if uid:
            unresolved_ids.add(uid)

    if unresolved_ids:
        try:
            uuid_ids = []
            for uid in unresolved_ids:
                try:
                    uuid_ids.append(_uuid.UUID(uid))
                except ValueError:
                    pass
            if uuid_ids:
                async with async_session() as db:
                    result = await db.execute(select(User.id, User.name).where(User.id.in_(uuid_ids)))
                    for u_id, u_name in result.all():
                        uid_to_name[str(u_id)] = u_name
        except Exception:
            optic.opt(exception=True).warning("User name resolution failed")

    _platform_names = {
        "kiro": "Kiro",
        "claude-code": "Claude Code",
        "cursor": "Cursor",
        "copilot-cli": "Copilot CLI",
        "codex-cli": "Codex CLI",
        "opencode": "OpenCode",
    }

    # Resolve agent names from PostgreSQL
    from models.agent import Agent

    agent_id_to_name: dict[str, str] = {}
    agent_ids_to_resolve: set[str] = set()
    for row in rows:
        aid = row.get("agent_id") or ""
        if aid:
            agent_ids_to_resolve.add(aid)

    if agent_ids_to_resolve:
        try:
            agent_uuids = []
            for aid in agent_ids_to_resolve:
                try:
                    agent_uuids.append(_uuid.UUID(aid))
                except ValueError:
                    pass
            if agent_uuids:
                async with async_session() as db:
                    result = await db.execute(select(Agent.id, Agent.name).where(Agent.id.in_(agent_uuids)))
                    for a_id, a_name in result.all():
                        agent_id_to_name[str(a_id)] = a_name
        except Exception:
            optic.opt(exception=True).warning("Agent name resolution failed")

    for row in rows:
        uid = row.get("user_id", "")
        row["user_name"] = uid_to_name.get(uid, current_user.name)
        harness = row.pop("harness", "") or ""
        row["platform"] = _platform_names.get(harness, harness.replace("-", " ").title() if harness else "Claude Code")
        row["service_name"] = harness
        row["is_active"] = bool(int(row.get("is_active", 0)))
        agent_id = row.get("agent_id") or None
        row["agent_id"] = agent_id if agent_id else None
        row["agent_name"] = agent_id_to_name.get(agent_id) if agent_id else None
        row["agent_version"] = row.get("agent_version") or None

    if status == "active":
        rows = [r for r in rows if r["is_active"]]
    return rows


async def _list_sessions_query(
    *,
    platform: str | None,
    user_ids: list[str] | None,
    days: int | None,
    is_admin: bool,
    uid: str,
    limit: int = 50,
    offset: int = 0,
    mine: bool = False,
) -> list[dict]:
    """Session list from ``session_stats_agg`` (one row per session)."""
    optic.trace("platform={}, days={}, is_admin={}", platform, days, is_admin)
    where_parts = ["session_id <> ''", "parent_session_id = ''", "prompt_count > 0"]
    params: dict[str, object] = {}

    if mine or not is_admin:
        where_parts.append("user_id = $uid")
        params["uid"] = uid
    if days is not None and days > 0:
        where_parts.append(f"last_event_time > {interval_ago('day', 'days')}")
        params["days"] = int(days)
    if platform:
        where_parts.append("harness = $platform")
        params["platform"] = platform
    if user_ids:
        condition = in_condition("user_id", user_ids, "user", params)
        if condition:
            where_parts.append(condition)
    params["limit"] = int(limit)
    params["offset"] = int(offset)

    where_clause = "WHERE " + " AND ".join(where_parts) + " "
    effective_last = f"CASE WHEN last_event_time < {FAR_FUTURE} THEN last_event_time ELSE first_event_time END"

    return await tq(
        "SELECT "
        "session_id, "
        f"CASE WHEN first_event_time > TIMESTAMP '2020-01-01 00:00:00' AND first_event_time < {FAR_FUTURE} "
        "     THEN first_event_time ELSE last_event_time END AS first_event_time, "
        f"{effective_last} AS last_event_time, "
        f"({effective_last} > {interval_ago('minute', 'active_minutes')}) AS is_active, "
        "prompt_count, "
        "0                   AS api_request_count, "
        "tool_result_count, "
        "input_tokens        AS total_input_tokens, "
        "output_tokens       AS total_output_tokens, "
        "cache_read_tokens   AS total_cache_read_tokens, "
        "cache_write_tokens  AS total_cache_write_tokens, "
        "total_credits, "
        "model, "
        "harness, "
        "agent_id, "
        "agent_version, "
        "user_id "
        "FROM session_stats_agg " + where_clause + "ORDER BY last_event_time DESC "
        "LIMIT $limit OFFSET $offset",
        {**params, "active_minutes": 5},
    )


@router.get("/summary")
async def sessions_summary(
    current_user: User = Depends(require_role(UserRole.user)),
):
    optic.trace("user_id={}", current_user.id)
    is_admin = _has_admin_trace_access(current_user)
    user_filter = ""
    params: dict[str, str] = {}
    if not is_admin:
        user_filter = "AND user_id = $uid "
        params["uid"] = str(current_user.id)

    rows = await tq(
        "SELECT "
        "count(*) AS total, "
        "count(*) FILTER (WHERE CAST(last_event_time AS DATE) = current_date) AS today_sessions "
        "FROM ( "
        "  SELECT session_id, max(last_event_time) AS last_event_time "
        "  FROM session_stats_agg "
        "  WHERE session_id <> '' " + user_filter + "  GROUP BY session_id "
        ")",
        params,
    )
    row = rows[0] if rows else {}
    return {
        "total_sessions": int(row.get("total", 0)),
        "today_sessions": int(row.get("today_sessions", 0)),
    }


@router.get("/stats")
@cache(expire=ds.get_sync_int("data.cache_ttl_default", 30), namespace="otel")
async def sessions_stats(current_user: User = Depends(require_role(UserRole.admin))):
    # prompt_count / tool_call_count correspond to 'user_prompt' / 'tool_call' event types.
    optic.trace("user_id={}", current_user.id)
    rows = await tq(
        "SELECT "
        "count(*) AS total_sessions, "
        "coalesce(sum(prompt_count), 0) AS total_prompts, "
        "0 AS total_api_requests, "
        "coalesce(sum(tool_call_count), 0) AS total_tool_calls, "
        "coalesce(sum(event_count), 0) AS total_events "
        "FROM ( "
        "  SELECT session_id, "
        "    sum(prompt_count) AS prompt_count, "
        "    sum(tool_call_count) AS tool_call_count, "
        "    sum(event_count) AS event_count "
        "  FROM session_stats_agg "
        "  WHERE session_id <> '' "
        "  GROUP BY session_id "
        ")"
    )
    row = rows[0] if rows else {}
    return {
        "total_sessions": int(row.get("total_sessions", 0)),
        "total_prompts": int(row.get("total_prompts", 0)),
        "total_api_requests": int(row.get("total_api_requests", 0)),
        "total_tool_calls": int(row.get("total_tool_calls", 0)),
        "total_events": int(row.get("total_events", 0)),
    }


@router.get("/{session_id}")
async def get_session(
    session_id: str,
    after_offset: int | None = Query(
        None, ge=0, description="Return only events after this line_offset (incremental fetch)"
    ),
    current_user: User = Depends(require_role(UserRole.user)),
):
    optic.trace("session_id={}, after_offset={}", session_id, after_offset)
    is_admin = _has_admin_trace_access(current_user)
    identity_params: dict[str, object] = {"sid": session_id}
    identity_user_filter = ""
    if not is_admin:
        identity_params["uid"] = str(current_user.id)
        identity_user_filter = "AND user_id = $uid "
    # Resolve the identity through the small summary table first so the wide
    # event scan below can filter on the integer session_key.
    identity_rows = await tq(
        "SELECT session_key, project_id, user_id, harness FROM session_stats_agg "
        "WHERE session_id = $sid " + identity_user_filter + "ORDER BY updated_at DESC LIMIT 1",
        identity_params,
    )
    if not identity_rows:
        identity_rows = await tq(
            "SELECT session_key, project_id, user_id, harness FROM session_events "
            "WHERE session_id = $sid " + identity_user_filter + "ORDER BY ingested_at DESC LIMIT 1",
            identity_params,
        )
    if not identity_rows:
        return {"session_id": session_id, "harness": "", "events": []}

    identity = identity_rows[0]
    params: dict[str, object] = {
        "sid": session_id,
        "key": int(identity["session_key"]),
        "pid": str(identity["project_id"]),
        "uid": str(identity["user_id"]),
        "harness": str(identity["harness"]),
    }

    # Build offset filter for incremental fetches
    _offset_filter = ""
    if after_offset is not None:
        _offset_filter = "AND line_offset::BIGINT > $offset "
        params["offset"] = int(after_offset)

    _main_sql = (
        "SELECT "
        "line_offset, timestamp, event_type, content_preview, tool_name, tool_id, "
        "uuid, parent_uuid, content_length, harness, agent_id, agent_version, raw_line, raw_line_truncated, "
        "credits, ingested_at "
        "FROM session_events "
        "WHERE session_key = $key AND session_id = $sid AND rendered " + _offset_filter + "ORDER BY line_offset ASC"
    )
    _sub_sql = (
        "SELECT session_id, timestamp, event_type, content_preview, "
        "tool_name, tool_id, uuid, parent_uuid, content_length, harness, "
        "raw_line, raw_line_truncated, credits, ingested_at, line_offset "
        "FROM session_events "
        "WHERE parent_session_key = $key AND parent_session_id = $sid AND project_id = $pid "
        "AND user_id = $uid AND harness = $harness AND rendered "
        + _offset_filter
        + "ORDER BY session_id, line_offset ASC"
    )
    rows, sub_rows_all = await asyncio.gather(tq(_main_sql, params), tq(_sub_sql, params))

    if not rows:
        if after_offset is not None:
            # Incremental fetch with no new data
            return {"session_id": session_id, "events": [], "max_offset": after_offset}
        return {"session_id": session_id, "service_name": "", "events": [], "traces": []}

    harness = rows[0].get("harness", "claude-code")
    agent_id = next((r.get("agent_id") for r in rows if r.get("agent_id")), None)
    agent_version = next((r.get("agent_version") for r in rows if r.get("agent_version")), None)
    agent_name = None
    if agent_id:
        try:
            from models.agent import Agent

            async with async_session() as db:
                result = await db.execute(select(Agent.name).where(Agent.id == _uuid.UUID(agent_id)))
                agent_name = result.scalar_one_or_none()
        except Exception:
            optic.opt(exception=True).warning("Agent name resolution failed")

    # Track max line_offset for incremental fetch cursor
    max_offset = max(int(r.get("line_offset", 0)) for r in rows) if rows else (after_offset or 0)

    # Parse raw events through the session parser for rich rendering
    from services.session_parsers import parse_raw_events

    events = parse_raw_events(rows)

    # sub_rows_all was fetched concurrently above alongside the main query.
    # Each subagent's first row carries parent_uuid pointing at the Agent
    # tool_call in the parent that spawned it - the frontend uses this to
    # nest the subagent inline at the right position.
    subagent_sessions = []
    if sub_rows_all:
        # Group by session_id
        from itertools import groupby

        sub_rows_all.sort(key=lambda r: r["session_id"])
        for sub_sid, sub_rows in groupby(sub_rows_all, key=lambda r: r["session_id"]):
            sub_rows_list = list(sub_rows)
            # first row's parent_uuid links to the Agent tool_call in the parent
            spawned_by = sub_rows_list[0].get("parent_uuid") if sub_rows_list else None
            sub_events = parse_raw_events(sub_rows_list)
            subagent_sessions.append(
                {
                    "session_id": sub_sid,
                    "spawned_by": spawned_by,
                    "events": sub_events,
                }
            )
    return {
        "session_id": session_id,
        "service_name": harness,
        "agent_id": agent_id,
        "agent_name": agent_name,
        "agent_version": agent_version,
        "events": events,
        "traces": [],
        "subagent_sessions": subagent_sessions,
        "max_offset": max_offset,
    }


@router.post("/{session_id}/bind-agent")
async def bind_session_agent(
    session_id: str,
    agent_name: str = Query(..., description="Agent name to bind to this session"),
    current_user: User = Depends(require_role(UserRole.user)),
):
    """Explicitly bind a session to an agent name."""
    optic.trace("session_id={}, agent_name={}", session_id, agent_name)
    is_admin = _is_admin_user(current_user)
    if not is_admin:
        params = {"sid": session_id, "uid": str(current_user.id)}
        ownership = await tq(
            "SELECT 1 FROM session_stats_agg WHERE session_id = $sid AND user_id = $uid LIMIT 1",
            params,
        )
        if not ownership:
            raise HTTPException(status_code=404, detail="Session not found or access denied")

    from redis.exceptions import RedisError

    from services.redis import get_redis

    try:
        redis = get_redis()
        await redis.set(f"session_agent:{session_id}", agent_name, ex=86400)
    except RedisError:
        return {"session_id": session_id, "agent_name": agent_name, "bound": False, "error": "Redis unavailable"}
    return {"session_id": session_id, "agent_name": agent_name, "bound": True}
