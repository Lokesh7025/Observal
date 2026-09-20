# SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Fixtures: an in-process telemetry store served over ASGI with a real DuckDB file."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest
import pytest_asyncio

from telemetry_store.api import create_app
from telemetry_store.settings import TelemetrySettings

if TYPE_CHECKING:
    from pathlib import Path

TOKEN = "test-token"


def make_settings(tmp_path: Path, **overrides) -> TelemetrySettings:
    base = dict(
        db_path=tmp_path / "telemetry.duckdb",
        temp_dir=tmp_path / "tmp",
        token=TOKEN,
        memory_limit="512MB",
        threads=2,
        read_threads=2,
        read_queue_max=4,
        query_timeout_ms=5_000,
        write_queue_timeout_ms=300,
        max_result_rows=10_000,
        checkpoint_interval_s=300,
        checkpoint_threshold="16MB",
        bind_host="127.0.0.1",
        bind_port=0,
        metrics_public=False,
        allow_anonymous=False,
    )
    base.update(overrides)
    return TelemetrySettings(**base)


@pytest.fixture()
def settings(tmp_path: Path) -> TelemetrySettings:
    return make_settings(tmp_path)


@pytest_asyncio.fixture()
async def store(settings: TelemetrySettings):
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://telemetry", headers={"Authorization": f"Bearer {TOKEN}"}
        ) as client:
            client.app = app  # type: ignore[attr-defined]
            yield client


@pytest.fixture()
def event_row():
    def _make(session_id: str = "s1", line_offset: int = 0, **overrides):
        row = {
            "session_id": session_id,
            "project_id": "default",
            "user_id": "u1",
            "harness": "claude-code",
            "agent_id": None,
            "agent_version": None,
            "layer_hash": None,
            "line_offset": line_offset,
            "source_end_offset": (line_offset + 1) * 100,
            "line_hash": f"h{line_offset}",
            "source_sha256": f"sha{line_offset}",
            "is_source_record": 1,
            "rendered": 1,
            "event_type": "user_prompt" if line_offset % 2 == 0 else "tool_call",
            "timestamp": f"2026-05-01 10:00:{line_offset % 60:02d}.000",
            "uuid": f"uuid-{line_offset}",
            "parent_uuid": None,
            "tool_name": None,
            "tool_id": None,
            "content_preview": "hello",
            "content_length": 5,
            "raw_line": '{"type":"user"}',
            "credits": 0.0,
            "parent_session_id": None,
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "model": "claude",
        }
        row.update(overrides)
        return row

    return _make
