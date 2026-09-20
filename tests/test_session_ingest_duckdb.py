# SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Ingest pipeline against a real telemetry store (acceptance A-18)."""

from __future__ import annotations

import hashlib
import json

import pytest

from services import session_ingest
from services.telemetry import TelemetryUnavailableError
from services.telemetry import client as tclient

PID, UID, HARNESS = "default", "11111111-1111-1111-1111-111111111111", "claude-code"


@pytest.fixture(autouse=True)
def _no_agent_resolution(monkeypatch):
    async def _same(agent_id):
        return agent_id

    async def _same_version(agent_id, version):
        return version

    monkeypatch.setattr(session_ingest, "_resolve_agent_id", _same)
    monkeypatch.setattr(session_ingest, "_resolve_agent_version", _same_version)


def _line(i: int, kind: str = "user") -> str:
    ts = f"2026-05-01T10:00:{i % 60:02d}.000Z"
    if kind == "user":
        return json.dumps(
            {"type": "user", "uuid": f"u{i}", "timestamp": ts, "message": {"role": "user", "content": f"prompt {i}"}}
        )
    return json.dumps(
        {
            "type": "assistant",
            "uuid": f"a{i}",
            "parentUuid": f"u{i - 1}",
            "timestamp": ts,
            "message": {
                "role": "assistant",
                "model": "claude-x",
                "usage": {"input_tokens": 10, "output_tokens": 5},
                "content": [{"type": "tool_use", "id": f"t{i}", "name": "Bash", "input": {}}],
            },
        }
    )


def _lines(n: int) -> list[str]:
    return [_line(i, "user" if i % 2 == 0 else "assistant") for i in range(n)]


async def _rows(telemetry, sql, **params):
    r = await telemetry.post("/v1/query", json={"sql": sql, "params": params})
    assert r.status_code == 200, r.text
    return r.json()["rows"]


async def test_ingest_writes_rows_summary_and_checkpoint(telemetry):
    lines = _lines(6)
    offsets = [(i + 1) * 100 for i in range(6)]
    result = await session_ingest.ingest_session_lines(
        session_id="s1",
        project_id=PID,
        user_id=UID,
        agent_id=None,
        agent_version=None,
        harness=HARNESS,
        lines=lines,
        start_offset=0,
        end_byte_offsets=offsets,
    )
    assert (result.ingested, result.skipped, result.errors) == (6, 0, 0)

    ack = await session_ingest.advance_session_checkpoint("s1", PID, UID, HARNESS)
    assert ack == (5, 600)

    rows = await _rows(
        telemetry,
        "SELECT line_offset, event_type, tool_name, uuid, parent_uuid, input_tokens FROM session_events ORDER BY line_offset",
    )
    assert [r["event_type"] for r in rows] == ["user_prompt", "tool_call"] * 3
    assert rows[1]["tool_name"] == "Bash" and rows[1]["parent_uuid"] == "u0" and rows[1]["input_tokens"] == 10

    summary = await _rows(
        telemetry,
        "SELECT prompt_count, tool_call_count, event_count, input_tokens, model, harness FROM session_stats_agg",
    )
    assert summary == [
        {
            "prompt_count": 3,
            "tool_call_count": 3,
            "event_count": 6,
            "input_tokens": 30,
            "model": "claude-x",
            "harness": HARNESS,
        }
    ]

    ck = await session_ingest.query_session_checkpoint("s1", PID, UID, HARNESS)
    assert ck == (5, 600)


async def test_exact_retry_is_skipped_and_conflict_at_checkpoint_is_rejected(telemetry):
    lines = _lines(4)
    await session_ingest.ingest_session_lines(
        session_id="s2",
        project_id=PID,
        user_id=UID,
        agent_id=None,
        agent_version=None,
        harness=HARNESS,
        lines=lines,
        start_offset=0,
    )
    await session_ingest.advance_session_checkpoint("s2", PID, UID, HARNESS)

    again = await session_ingest.ingest_session_lines(
        session_id="s2",
        project_id=PID,
        user_id=UID,
        agent_id=None,
        agent_version=None,
        harness=HARNESS,
        lines=lines,
        start_offset=0,
    )
    assert (again.ingested, again.skipped) == (0, 4)
    assert await _rows(telemetry, "SELECT count(*) AS c FROM session_events") == [{"c": 4}]

    changed = [lines[0], _line(1, "user") + " ", lines[2], lines[3]]
    with pytest.raises(session_ingest.SessionRecordConflictError) as exc:
        await session_ingest.ingest_session_lines(
            session_id="s2",
            project_id=PID,
            user_id=UID,
            agent_id=None,
            agent_version=None,
            harness=HARNESS,
            lines=changed,
            start_offset=0,
        )
    assert exc.value.offsets == [1]


async def test_gap_then_fill_advances_checkpoint_and_integrity(telemetry):
    lines = _lines(8)
    offsets = [(i + 1) * 10 for i in range(8)]
    # Lines 0-2 first, then 5-7 (gap), then 3-4.
    for lo, hi in ((0, 3), (5, 8), (3, 5)):
        await session_ingest.ingest_session_lines(
            session_id="s3",
            project_id=PID,
            user_id=UID,
            agent_id=None,
            agent_version=None,
            harness=HARNESS,
            lines=lines[lo:hi],
            start_offset=lo,
            end_byte_offsets=offsets[lo:hi],
        )
        ack = await session_ingest.advance_session_checkpoint("s3", PID, UID, HARNESS)
        if hi == 3 or lo == 5:
            assert ack == (2, 30)
        else:
            assert ack == (7, 80)

    hasher = hashlib.sha256()
    for line in lines:
        hasher.update(hashlib.sha256(line.encode()).hexdigest().encode())
        hasher.update(b"\n")
    integrity = await session_ingest.check_session_integrity(
        "s3",
        PID,
        UID,
        HARNESS,
        expected_line_count=8,
        expected_offset=80,
        expected_hash=hasher.hexdigest(),
        hashed_line_count=8,
    )
    assert integrity.ok and integrity.server_hash == hasher.hexdigest() and integrity.repair_from_line is None

    bad = await session_ingest.check_session_integrity(
        "s3",
        PID,
        UID,
        HARNESS,
        expected_line_count=9,
        expected_offset=90,
        expected_hash="nope",
        hashed_line_count=9,
    )
    assert not bad.ok and bad.repair_from_line == 8 and bad.repair_offset == 80


async def test_kiro_credits_row_and_parse_errors(telemetry):
    lines = [_line(0), "not json", _line(2)]
    result = await session_ingest.ingest_session_lines(
        session_id="k1",
        project_id=PID,
        user_id=UID,
        agent_id=None,
        agent_version=None,
        harness="kiro",
        lines=lines,
        start_offset=0,
        total_credits=3.5,
    )
    assert result.errors == 1
    rows = await _rows(
        telemetry,
        "SELECT line_offset, event_type, rendered, is_source_record, credits FROM session_events ORDER BY line_offset",
    )
    assert rows[1]["event_type"] == "_parse_error" and rows[1]["rendered"] is False
    assert (
        rows[-1]["line_offset"] == 0xFFFFFFFF
        and rows[-1]["event_type"] == "kiro_credits"
        and rows[-1]["is_source_record"] is False
    )
    summary = await _rows(telemetry, "SELECT event_count, total_credits FROM session_stats_agg")
    assert summary == [{"event_count": 3, "total_credits": 3.5}]  # parse error row is not rendered; credits row is


async def test_store_outage_raises_and_writes_nothing(telemetry):
    import httpx

    class _Down(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise httpx.ConnectError("down")

    tclient.set_client(httpx.AsyncClient(transport=_Down(), base_url="http://telemetry"))
    with pytest.raises(TelemetryUnavailableError):
        await session_ingest.ingest_session_lines(
            session_id="s9",
            project_id=PID,
            user_id=UID,
            agent_id=None,
            agent_version=None,
            harness=HARNESS,
            lines=_lines(2),
            start_offset=0,
        )
