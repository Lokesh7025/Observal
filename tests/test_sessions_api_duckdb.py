# SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Sessions API against a real telemetry store (acceptance A-13)."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from api.routes import sessions
from models.user import UserRole

USER = uuid.UUID("11111111-1111-1111-1111-111111111111")
OTHER = uuid.UUID("22222222-2222-2222-2222-222222222222")
PID = "default"


def _user(role=UserRole.user, user_id=USER, trace_privacy=False):
    return SimpleNamespace(
        id=user_id, role=role, name="u", email="u@x", auth_provider="local", _trace_privacy=trace_privacy
    )


def _claude_line(i: int, kind: str, ts: str, parent_uuid: str | None = None) -> str:
    base = {"uuid": f"u{i}", "timestamp": ts, "parentUuid": parent_uuid}
    if kind == "user_prompt":
        base.update({"type": "user", "message": {"role": "user", "content": f"prompt {i}"}})
    else:
        base.update(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "model": "claude-x",
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                    "content": [{"type": "tool_use", "id": f"t{i}", "name": "Bash", "input": {}}],
                },
            }
        )
    return json.dumps(base)


async def _seed_session(
    telemetry, session_id: str, user_id: uuid.UUID, ts: str, *, harness="claude-code", n=4, parent: str | None = None
):
    rows = []
    for i in range(n):
        kind = "user_prompt" if i % 2 == 0 else "tool_call"
        rows.append(
            {
                "session_id": session_id,
                "project_id": PID,
                "user_id": str(user_id),
                "harness": harness,
                "line_offset": i,
                "line_hash": f"h{i}",
                "event_type": kind,
                "timestamp": ts,
                "uuid": f"u{i}",
                "parent_uuid": "spawn" if (parent and i == 0) else None,
                "tool_name": "Bash" if kind == "tool_call" else None,
                "raw_line": _claude_line(i, kind, ts.replace(" ", "T") + "Z"),
                "content_preview": f"p{i}",
                "input_tokens": 10 if kind == "tool_call" else 0,
                "output_tokens": 5 if kind == "tool_call" else 0,
                "model": "claude-x" if kind == "tool_call" else "",
                "parent_session_id": parent,
            }
        )
    r = await telemetry.post(
        "/v1/write/session-batch",
        json={
            "events": rows,
            "advance_checkpoint": {
                "project_id": PID,
                "user_id": str(user_id),
                "harness": harness,
                "session_id": session_id,
            },
        },
    )
    assert r.status_code == 200, r.text


@pytest.fixture(autouse=True)
def _no_postgres(monkeypatch):
    class _Ctx:
        async def __aenter__(self):
            raise AssertionError("postgres should not be needed")

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(sessions, "async_session", lambda: _Ctx())


async def test_list_summary_stats_and_scoping(telemetry, monkeypatch):
    await _seed_session(telemetry, "mine-1", USER, "2026-05-01 10:00:00.000")
    await _seed_session(telemetry, "mine-2", USER, "2026-05-02 10:00:00.000", harness="kiro")
    await _seed_session(telemetry, "theirs", OTHER, "2026-05-03 10:00:00.000")
    await _seed_session(telemetry, "child", USER, "2026-05-01 10:00:00.000", parent="mine-1")

    # Non-admin sees only own top-level sessions, newest first.
    rows = await sessions._list_sessions_query(
        platform=None, user_ids=None, days=None, is_admin=False, uid=str(USER), limit=50, offset=0
    )
    assert [r["session_id"] for r in rows] == ["mine-2", "mine-1"]
    row = rows[1]
    assert row["prompt_count"] == 2 and row["tool_result_count"] == 0
    assert row["total_input_tokens"] == 20 and row["model"] == "claude-x"
    assert row["first_event_time"] == "2026-05-01 10:00:00.000"
    assert row["is_active"] is False
    assert row["api_request_count"] == 0

    # Admin sees everyone; harness and pagination filters apply.
    rows = await sessions._list_sessions_query(
        platform=None, user_ids=None, days=None, is_admin=True, uid=str(USER), limit=2, offset=1
    )
    assert [r["session_id"] for r in rows] == ["mine-2", "mine-1"]
    rows = await sessions._list_sessions_query(
        platform="kiro", user_ids=[str(USER), str(OTHER)], days=None, is_admin=True, uid=str(USER)
    )
    assert [r["session_id"] for r in rows] == ["mine-2"]
    rows = await sessions._list_sessions_query(platform=None, user_ids=None, days=1, is_admin=True, uid=str(USER))
    assert rows == []

    summary = await sessions.sessions_summary(_user())
    assert summary == {"total_sessions": 3, "today_sessions": 0}  # includes the subagent row
    summary = await sessions.sessions_summary(_user(UserRole.super_admin))
    assert summary["total_sessions"] == 4

    stats = await sessions.sessions_stats.__wrapped__(current_user=_user(UserRole.admin))
    assert stats == {
        "total_sessions": 4,
        "total_prompts": 8,
        "total_api_requests": 0,
        "total_tool_calls": 8,
        "total_events": 16,
    }


async def test_detail_with_subagents_incremental_and_isolation(telemetry):
    await _seed_session(telemetry, "parent", USER, "2026-05-01 10:00:00.000")
    await _seed_session(telemetry, "child", USER, "2026-05-01 10:00:01.000", n=2, parent="parent")

    result = await sessions.get_session("parent", after_offset=None, current_user=_user())
    assert result["session_id"] == "parent" and result["service_name"] == "claude-code"
    assert result["max_offset"] == 3
    prompts = [e for e in result["events"] if e.get("event_name") == "hook_userpromptsubmit"]
    assert len(prompts) == 2
    assert [s["session_id"] for s in result["subagent_sessions"]] == ["child"]
    assert result["subagent_sessions"][0]["spawned_by"] == "spawn"
    assert result["subagent_sessions"][0]["events"]

    incremental = await sessions.get_session("parent", after_offset=2, current_user=_user())
    assert incremental["events"] and incremental["max_offset"] == 3
    assert not [e for e in incremental["events"] if e.get("event_name") == "hook_userpromptsubmit"]
    empty = await sessions.get_session("parent", after_offset=3, current_user=_user())
    assert empty == {"session_id": "parent", "events": [], "max_offset": 3}

    # Another user cannot see it; an admin can.
    denied = await sessions.get_session("parent", after_offset=None, current_user=_user(user_id=OTHER))
    assert denied == {"session_id": "parent", "harness": "", "events": []}
    admin = await sessions.get_session("parent", after_offset=None, current_user=_user(UserRole.admin, user_id=OTHER))
    assert admin["max_offset"] == 3 and admin["events"]


async def test_detail_paginates_parent_and_subagent_events(telemetry, monkeypatch):
    """Session detail must read past the store row cap without gaps or duplicates."""
    monkeypatch.setattr(sessions, "_DETAIL_PAGE_SIZE", 2)
    # Match the store cap to the page size: any unbounded query in this test
    # returns 413 instead of silently passing against the fixture's 100k cap.
    telemetry._transport.app.state.telemetry.reader._max_rows = 2
    monkeypatch.setattr(
        "services.session_parsers.parse_raw_events",
        lambda rows: [{"line_offset": int(row["line_offset"])} for row in rows],
    )

    await _seed_session(telemetry, "parent-paged", USER, "2026-05-01 10:00:00.000", n=5)
    await _seed_session(
        telemetry,
        "child-a",
        USER,
        "2026-05-01 10:00:01.000",
        n=3,
        parent="parent-paged",
    )
    await _seed_session(
        telemetry,
        "child-b",
        USER,
        "2026-05-01 10:00:02.000",
        n=3,
        parent="parent-paged",
    )

    result = await sessions.get_session("parent-paged", after_offset=None, current_user=_user())

    assert [event["line_offset"] for event in result["events"]] == [0, 1, 2, 3, 4]
    assert result["max_offset"] == 4
    assert [child["session_id"] for child in result["subagent_sessions"]] == ["child-a", "child-b"]
    assert [event["line_offset"] for event in result["subagent_sessions"][0]["events"]] == [0, 1, 2]
    assert [event["line_offset"] for event in result["subagent_sessions"][1]["events"]] == [0, 1, 2]

    incremental = await sessions.get_session("parent-paged", after_offset=0, current_user=_user())
    assert [event["line_offset"] for event in incremental["events"]] == [1, 2, 3, 4]
    assert [child["session_id"] for child in incremental["subagent_sessions"]] == ["child-a", "child-b"]
    assert [event["line_offset"] for event in incremental["subagent_sessions"][0]["events"]] == [1, 2]
    assert [event["line_offset"] for event in incremental["subagent_sessions"][1]["events"]] == [1, 2]
    assert incremental["max_offset"] == 4


async def test_store_outage_raises_instead_of_returning_empty(telemetry):
    import httpx

    from services.telemetry import TelemetryUnavailableError
    from services.telemetry import client as tclient

    class _Down(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise httpx.ConnectError("down")

    tclient.set_client(httpx.AsyncClient(transport=_Down(), base_url="http://telemetry"))
    with pytest.raises(TelemetryUnavailableError):
        await sessions._list_sessions_query(platform=None, user_ids=None, days=None, is_admin=True, uid=str(USER))
    with pytest.raises(TelemetryUnavailableError):
        await sessions.sessions_summary(_user())
    with pytest.raises(TelemetryUnavailableError):
        await sessions.get_session("x", after_offset=None, current_user=_user())
