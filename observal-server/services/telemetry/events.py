# SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Session-event reads and writes used by the ingest pipeline."""

from __future__ import annotations

import time
from typing import Any

from loguru import logger as optic

from observal_shared.telemetry_keys import session_key
from services.telemetry import client
from services.telemetry.sql import normalize_ts

_IDENTITY_WHERE = "session_key = $key AND session_id = $sid"


def _identity_params(session_id: str, project_id: str, user_id: str, harness: str) -> dict[str, Any]:
    return {"key": session_key(project_id, user_id, harness, session_id), "sid": session_id}


def _prepare_event_rows(rows: list[dict]) -> list[dict]:
    for row in rows:
        row.setdefault("source_end_offset", 0)
        row.setdefault("is_source_record", 1)
        row.setdefault("rendered", 1)
        row.setdefault("raw_line_truncated", 0)
        if "timestamp" in row:
            row["timestamp"] = normalize_ts(row["timestamp"])
    return rows


async def insert_session_batch(
    rows: list[dict],
    *,
    session_id: str,
    project_id: str,
    user_id: str,
    harness: str,
    refresh_summary: bool = True,
    advance_checkpoint: bool = True,
) -> dict[str, Any]:
    """Replace events, refresh the session summary, and advance the checkpoint in one transaction."""
    payload: dict[str, Any] = {"events": _prepare_event_rows(rows), "refresh_summary": refresh_summary}
    if advance_checkpoint:
        payload["advance_checkpoint"] = {
            "project_id": project_id,
            "user_id": user_id,
            "harness": harness,
            "session_id": session_id,
        }
    try:
        return await client.write("/v1/write/session-batch", payload)
    except client.TelemetryError as exc:
        optic.error("failed to write {} session events for session {} - {}", len(rows), session_id, exc)
        raise


async def insert_session_events(rows: list[dict]) -> None:
    """Replace canonical session rows without touching summaries (migration/backfill paths)."""
    if not rows:
        return
    await client.write("/v1/write/replace", {"table": "session_events", "rows": _prepare_event_rows(rows)})


async def refresh_session_summary(session_id: str, project_id: str, user_id: str, harness: str) -> None:
    await client.write(
        "/v1/write/session-batch",
        {
            "events": [],
            "refresh_summary": True,
            "advance_checkpoint": {
                "project_id": project_id,
                "user_id": user_id,
                "harness": harness,
                "session_id": session_id,
            },
        },
    )


async def insert_session_checkpoint(
    session_id: str,
    project_id: str,
    user_id: str,
    harness: str,
    acknowledged_line: int,
    acknowledged_offset: int,
) -> None:
    """Write an explicit checkpoint, including audit rewinds."""
    await client.write(
        "/v1/write/checkpoint",
        {
            "project_id": project_id,
            "user_id": user_id,
            "harness": harness,
            "session_id": session_id,
            "acknowledged_line": int(acknowledged_line),
            "acknowledged_offset": int(acknowledged_offset),
        },
    )


async def query_session_checkpoint(session_id: str, project_id: str, user_id: str, harness: str) -> tuple[int, int]:
    """Return the durable contiguous (source line, end byte) checkpoint."""
    rows = await client.query(
        "SELECT acknowledged_line, acknowledged_offset FROM session_checkpoints "
        f"WHERE {_IDENTITY_WHERE} ORDER BY checkpoint_version DESC LIMIT 1",
        _identity_params(session_id, project_id, user_id, harness),
    )
    if not rows:
        return -1, 0
    return int(rows[0]["acknowledged_line"]), int(rows[0].get("acknowledged_offset") or 0)


async def query_source_records_after(
    session_id: str,
    project_id: str,
    user_id: str,
    harness: str,
    after_line: int,
    limit: int = 5000,
) -> list[tuple[int, int]]:
    """Return ordered source positions after a checkpoint for gap detection."""
    params = _identity_params(session_id, project_id, user_id, harness)
    params.update({"after": int(after_line), "limit": int(limit)})
    rows = await client.query(
        "SELECT line_offset, source_end_offset FROM session_events "
        f"WHERE {_IDENTITY_WHERE} AND is_source_record AND line_offset::BIGINT > $after "
        "ORDER BY line_offset LIMIT $limit",
        params,
    )
    return [(int(r["line_offset"]), int(r.get("source_end_offset") or 0)) for r in rows]


async def query_session_source_manifest(
    session_id: str,
    project_id: str,
    user_id: str,
    harness: str,
) -> list[tuple[int, int, str]]:
    """Return canonical source positions for final integrity auditing."""
    rows = await client.query(
        "SELECT line_offset, source_end_offset, source_sha256 FROM session_events "
        f"WHERE {_IDENTITY_WHERE} AND is_source_record ORDER BY line_offset",
        _identity_params(session_id, project_id, user_id, harness),
    )
    return [
        (int(r["line_offset"]), int(r.get("source_end_offset") or 0), str(r.get("source_sha256") or "")) for r in rows
    ]


async def query_existing_for_dedup(
    session_id: str,
    project_id: str,
    user_id: str,
    harness: str,
    min_offset: int,
    max_offset: int,
) -> dict[int, str]:
    """Return existing source line hashes by stable source index."""
    t0 = time.perf_counter()
    if min_offset > max_offset:
        return {}
    params = _identity_params(session_id, project_id, user_id, harness)
    params.update({"min_off": int(min_offset), "max_off": int(max_offset)})
    rows = await client.query(
        "SELECT line_offset, line_hash FROM session_events "
        f"WHERE {_IDENTITY_WHERE} AND is_source_record "
        "AND line_offset::BIGINT >= $min_off AND line_offset::BIGINT <= $max_off",
        params,
    )
    existing = {int(r["line_offset"]): str(r.get("line_hash") or "") for r in rows}
    optic.trace(
        "dedup check for session {}: {} offsets in range [{}, {}] ({:.0f}ms)",
        session_id,
        len(existing),
        min_offset,
        max_offset,
        (time.perf_counter() - t0) * 1000,
    )
    return existing
