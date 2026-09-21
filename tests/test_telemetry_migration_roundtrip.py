# SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Migration tooling against a real telemetry store (acceptance A-08/A-09/A-10/A-19 subset).

Simulates a legacy ClickHouse export (Parquet chunks + 2.0 manifest), imports
it, checks idempotent resume, live-overlap protection, derived rebuild, then
exports the store (3.0 manifest) and re-imports into a second store.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import observal_shared.migration.telemetry_import as telemetry_import_module
from observal_shared.migration import (
    ArtifactValidationError,
    NullReporter,
    TelemetryConnParams,
    export_telemetry,
    import_telemetry,
    validate_telemetry,
)
from observal_shared.migration.legacy_clickhouse_export import TelemetryChunk
from observal_shared.migration.telemetry_import import manifest_chunks
from telemetry_store.api import create_app
from telemetry_store.settings import TelemetrySettings

if TYPE_CHECKING:
    from pathlib import Path

TOKEN = "test-token"


def _event(session_id: str, i: int, *, user="u1", ingested="2026-03-01 00:00:01.000") -> dict:
    return {
        "session_id": session_id,
        "project_id": "default",
        "user_id": user,
        "harness": "claude-code",
        "agent_id": None,
        "agent_version": None,
        "layer_hash": None,
        "line_offset": i,
        "source_end_offset": (i + 1) * 10,
        "line_hash": f"h{i}",
        "source_sha256": f"s{i}",
        "is_source_record": 1,
        "rendered": 1,
        "event_type": "user_prompt" if i % 2 == 0 else "tool_call",
        "timestamp": f"2026-03-01 00:00:{i % 60:02d}.000",
        "uuid": f"u{i}",
        "parent_uuid": None,
        "tool_name": None,
        "tool_id": None,
        "content_preview": "p",
        "content_length": 1,
        "raw_line": "{}",
        "ingested_at": ingested,
        "credits": 0.0,
        "parent_session_id": None,
        "input_tokens": 1,
        "output_tokens": 1,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "model": "m",
        "raw_line_truncated": 0,
    }


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_legacy_export(root: Path, migration_id: str = "cutover-test") -> dict:
    """Write a ClickHouse-style 2.0 telemetry export."""
    root.mkdir(parents=True, exist_ok=True)
    tables: dict = {}

    start = datetime(2026, 3, 1, tzinfo=UTC)
    end = datetime(2026, 4, 1, tzinfo=UTC)

    def add_chunk(table: str, bucket: int, rows: list[dict]):
        chunk = TelemetryChunk(table, start, end, bucket, 2)
        name = chunk.filename
        path = root / name
        pq.write_table(pa.Table.from_pylist(rows), path)
        meta = tables.setdefault(table, {"files": [], "row_count": 0, "checksum": {}, "time_range": None, "chunks": []})
        meta["files"].append(name)
        meta["row_count"] += len(rows)
        meta["checksum"][name] = _sha(path)
        meta["chunks"].append(
            {
                "chunk_id": chunk.chunk_id,
                "file": name,
                "sha256": _sha(path),
                "row_count": len(rows),
                "size_bytes": path.stat().st_size,
                "range_start": "2026-03-01 00:00:00.000",
                "range_end": "2026-04-01 00:00:00.000",
                "bucket": bucket,
                "shard_count": 2,
            }
        )
        return name

    session_a = add_chunk("session_events", 0, [_event("s1", i) for i in range(5)])
    add_chunk("session_events", 1, [_event("s2", i, user="u2") for i in range(3)])
    add_chunk(
        "audit_log",
        0,
        [
            {
                "event_id": "11111111-1111-1111-1111-111111111111",
                "timestamp": "2026-03-01 00:00:00.000",
                "actor_id": "u1",
                "action": "login",
            }
        ],
    )
    add_chunk(
        "layer_snapshots",
        0,
        [
            {
                "hash": "abc",
                "project_id": "default",
                "user_id": "u1",
                "harness": "pi",
                "content": "{}",
                "uploaded_at": "2026-03-01 00:00:00.000",
                "file_count": 1,
                "total_size": 2,
                "lockfile_hash": "",
            }
        ],
    )
    # Derived tables appear in ClickHouse exports; the importer must ignore them.
    add_chunk("session_stats_agg", 0, [{"project_id": "default", "session_id": "stale"}])
    for table in ("session_checkpoints", "security_events", "webhook_deliveries"):
        tables[table] = {"files": [], "row_count": 0, "checksum": {}, "time_range": None, "chunks": []}

    manifest = {
        "schema_version": "2.0",
        "migration_id": migration_id,
        "phase": "deep_copy",
        "phase_status": "export_complete",
        "export_completed_at": "2026-03-02T00:00:00+00:00",
        "export_time_cutoff": "2026-03-02 00:00:00.000",
        "tables": tables,
    }
    (root / "telemetry_manifest.json").write_text(json.dumps(manifest, indent=2))
    manifest["_session_chunk_a"] = session_a
    return manifest


class LiveStore:
    """A real telemetry store served over ASGI with a routed httpx client."""

    def __init__(self, tmp_path: Path, name: str = "store"):
        self.name = name
        self.settings = TelemetrySettings(
            db_path=tmp_path / "telemetry.duckdb",
            temp_dir=tmp_path / "tmp",
            export_dir=tmp_path / "exports",
            token=TOKEN,
            memory_limit="512MB",
            threads=2,
            read_threads=2,
            read_queue_max=16,
            query_timeout_ms=10_000,
            write_queue_timeout_ms=1_000,
            max_result_rows=100_000,
            checkpoint_interval_s=300,
            checkpoint_threshold="16MB",
            bind_host="127.0.0.1",
            bind_port=0,
            metrics_public=False,
            allow_anonymous=False,
        )
        self.app = create_app(self.settings)
        self.params = TelemetryConnParams(url=f"http://{name}", token=TOKEN)

    async def __aenter__(self):
        self._lifespan = self.app.router.lifespan_context(self.app)
        await self._lifespan.__aenter__()
        return self

    async def __aexit__(self, *exc):
        await self._lifespan.__aexit__(*exc)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url=f"http://{self.name}")

    async def rows(self, sql: str, **params) -> list[dict]:
        async with self.client() as c:
            r = await c.post("/v1/query", json={"sql": sql, "params": params}, headers=self.params.headers)
            assert r.status_code == 200, r.text
            return r.json()["rows"]


@pytest.fixture()
def route_httpx_to_stores(monkeypatch):
    """Route ``httpx.AsyncClient`` calls made by the migration package to in-process stores."""
    stores: dict[str, LiveStore] = {}
    real_client = httpx.AsyncClient

    class RoutedClient(real_client):
        def __init__(self, *args, **kwargs):
            kwargs.pop("transport", None)
            kwargs["transport"] = _MultiTransport(stores)
            super().__init__(*args, **kwargs)

    class _MultiTransport(httpx.AsyncBaseTransport):
        def __init__(self, table):
            self.table = table

        async def handle_async_request(self, request):
            store = self.table[request.url.host]
            transport = httpx.ASGITransport(app=store.app)
            return await transport.handle_async_request(request)

    monkeypatch.setattr(httpx, "AsyncClient", RoutedClient)
    return stores


async def test_import_is_idempotent_rebuilds_derived_and_protects_live_rows(tmp_path, route_httpx_to_stores):
    export_dir = tmp_path / "legacy"
    write_legacy_export(export_dir)

    async with LiveStore(tmp_path / "a") as store:
        route_httpx_to_stores["store"] = store
        # A live session ingested *after* the export cutoff must survive the backfill.
        async with store.client() as c:
            live = _event("s1", 0, ingested="2026-05-01 00:00:00.000")
            live["content_preview"] = "LIVE"
            r = await c.post(
                "/v1/write/session-batch",
                json={
                    "events": [live],
                    "advance_checkpoint": {
                        "project_id": "default",
                        "user_id": "u1",
                        "harness": "claude-code",
                        "session_id": "s1",
                    },
                },
                headers=store.params.headers,
            )
            assert r.status_code == 200, r.text

        result = await import_telemetry(store.params, export_dir, NullReporter())
        assert result.tables_imported == {"session_events": 8, "audit_log": 1, "layer_snapshots": 1}
        assert result.failed_files == []
        assert result.derived_rebuild["sessions"] == 2

        rows = await store.rows("SELECT session_id, count(*) AS c FROM session_events GROUP BY session_id ORDER BY 1")
        assert rows == [{"session_id": "s1", "c": 5}, {"session_id": "s2", "c": 3}]
        live_row = await store.rows(
            "SELECT content_preview FROM session_events WHERE session_id='s1' AND line_offset=0"
        )
        assert live_row == [{"content_preview": "LIVE"}]
        assert await store.rows("SELECT count(*) AS c FROM session_stats_agg WHERE session_id = 'stale'") == [{"c": 0}]
        summaries = await store.rows("SELECT session_id, event_count, prompt_count FROM session_stats_agg ORDER BY 1")
        assert summaries == [
            {"session_id": "s1", "event_count": 5, "prompt_count": 3},
            {"session_id": "s2", "event_count": 3, "prompt_count": 2},
        ]
        checkpoints = await store.rows("SELECT session_id, acknowledged_line FROM session_checkpoints ORDER BY 1")
        assert checkpoints == [
            {"session_id": "s1", "acknowledged_line": 4},
            {"session_id": "s2", "acknowledged_line": 2},
        ]

        # Re-running (resume) imports nothing new and keeps counts stable.
        again = await import_telemetry(store.params, export_dir, NullReporter())
        assert again.rows_imported == 10
        assert await store.rows("SELECT count(*) AS c FROM session_events") == [{"c": 8}]
        assert await store.rows("SELECT count(*) AS c FROM telemetry_import_ledger") == [{"c": 4}]
        # Resume state file exists and is scoped to the migration id.
        state = json.loads((export_dir / "import_state.json").read_text())
        assert state["migration_id"] == "cutover-test" and len(state["completed"]) == 4
        assert all({"table", "sha256", "row_count"} <= entry.keys() for entry in state["completed"].values())

        validation = await validate_telemetry(store.params, None, export_dir, NullReporter())
        assert validation.checksums_valid
        assert validation.row_count_results["session_events"] == (8, 8)
        assert validation.row_count_results["audit_log"] == (1, 1)


async def test_checksum_mismatch_is_reported_before_import(tmp_path, route_httpx_to_stores):
    export_dir = tmp_path / "legacy"
    manifest = write_legacy_export(export_dir)
    (export_dir / manifest["_session_chunk_a"]).write_bytes(b"corrupt")

    async with LiveStore(tmp_path / "a") as store:
        route_httpx_to_stores["store"] = store
        from observal_shared.migration import MigrationError

        with pytest.raises(MigrationError, match="checksum mismatch"):
            await import_telemetry(store.params, export_dir, NullReporter())
        # The entire manifest is checked before the destination is contacted for writes.
        assert await store.rows("SELECT count(*) AS c FROM session_events") == [{"c": 0}]
        assert await store.rows("SELECT count(*) AS c FROM telemetry_import_ledger") == [{"c": 0}]
        validation = await validate_telemetry(None, None, export_dir, NullReporter())
        assert validation.checksums_valid is False


async def test_interrupted_import_resumes_without_duplicate_writes(tmp_path, route_httpx_to_stores, monkeypatch):
    export_dir = tmp_path / "legacy"
    write_legacy_export(export_dir)
    async with LiveStore(tmp_path / "a") as store:
        route_httpx_to_stores["store"] = store
        original_post_chunk = telemetry_import_module._post_chunk
        calls = 0

        async def interrupt_after_first(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise telemetry_import_module.MigrationError("simulated interruption")
            return await original_post_chunk(*args, **kwargs)

        monkeypatch.setattr(telemetry_import_module, "_post_chunk", interrupt_after_first)
        with pytest.raises(telemetry_import_module.MigrationError, match="simulated interruption"):
            await import_telemetry(store.params, export_dir, NullReporter(), rebuild=False)
        assert await store.rows("SELECT count(*) AS c FROM telemetry_import_ledger") == [{"c": 1}]

        monkeypatch.setattr(telemetry_import_module, "_post_chunk", original_post_chunk)
        result = await import_telemetry(store.params, export_dir, NullReporter(), rebuild=False)
        assert result.rows_imported == 10
        assert await store.rows("SELECT count(*) AS c FROM session_events") == [{"c": 8}]
        assert await store.rows("SELECT count(*) AS c FROM telemetry_import_ledger") == [{"c": 4}]


async def test_modified_resume_state_is_rejected(tmp_path, route_httpx_to_stores):
    export_dir = tmp_path / "legacy"
    write_legacy_export(export_dir)
    async with LiveStore(tmp_path / "a") as store:
        route_httpx_to_stores["store"] = store
        await import_telemetry(store.params, export_dir, NullReporter(), rebuild=False)
        state_path = export_dir / "import_state.json"
        state = json.loads(state_path.read_text())
        first = next(iter(state["completed"].values()))
        first["row_count"] += 1
        state_path.write_text(json.dumps(state))

        with pytest.raises(ArtifactValidationError, match="resume state metadata changed"):
            await import_telemetry(store.params, export_dir, NullReporter(), rebuild=False)
        assert await store.rows("SELECT count(*) AS c FROM telemetry_import_ledger") == [{"c": 4}]


@pytest.mark.parametrize(
    "damage", ["duplicate_id", "duplicate_file", "traversal", "missing_file", "table_total", "parquet_row_count"]
)
async def test_invalid_manifest_is_rejected_before_any_rows_are_written(tmp_path, route_httpx_to_stores, damage):
    export_dir = tmp_path / "legacy"
    manifest = write_legacy_export(export_dir)
    session_chunks = manifest["tables"]["session_events"]["chunks"]
    if damage == "duplicate_id":
        session_chunks[1]["chunk_id"] = session_chunks[0]["chunk_id"]
    elif damage == "duplicate_file":
        session_chunks[1]["file"] = session_chunks[0]["file"]
    elif damage == "traversal":
        session_chunks[0]["file"] = "../outside.parquet"
    elif damage == "missing_file":
        (export_dir / session_chunks[0]["file"]).unlink()
    elif damage == "table_total":
        manifest["tables"]["session_events"]["row_count"] += 1
    else:
        session_chunks[1]["row_count"] += 1
        manifest["tables"]["session_events"]["row_count"] += 1
    manifest.pop("_session_chunk_a", None)
    (export_dir / "telemetry_manifest.json").write_text(json.dumps(manifest, indent=2))

    async with LiveStore(tmp_path / "a") as store:
        route_httpx_to_stores["store"] = store
        with pytest.raises(ArtifactValidationError):
            await import_telemetry(store.params, export_dir, NullReporter(), rebuild=False)
        assert await store.rows("SELECT count(*) AS c FROM session_events") == [{"c": 0}]
        assert await store.rows("SELECT count(*) AS c FROM telemetry_import_ledger") == [{"c": 0}]


async def test_store_export_roundtrips_into_a_second_store(tmp_path, route_httpx_to_stores):
    legacy = tmp_path / "legacy"
    write_legacy_export(legacy)
    async with LiveStore(tmp_path / "a", "source") as source, LiveStore(tmp_path / "b", "target") as target:
        route_httpx_to_stores["source"] = source
        route_httpx_to_stores["target"] = target
        await import_telemetry(source.params, legacy, NullReporter())

        out = tmp_path / "export"
        result = await export_telemetry(source.params, out, NullReporter(), migration_id="s2s-1")
        assert result.total_rows == 10
        manifest = json.loads((out / "telemetry_manifest.json").read_text())
        assert manifest["telemetry_manifest_version"] == "3.0"
        chunks = manifest_chunks(manifest, out)
        # Only tables with rows produce chunk files; empty ones appear in the manifest with row_count=0.
        assert {c["table"] for c in chunks} == {"session_events", "audit_log", "layer_snapshots"}
        assert manifest["tables"]["security_events"] == {"row_count": 0}
        assert manifest["tables"]["webhook_deliveries"] == {"row_count": 0}
        for chunk in chunks:
            assert _sha(chunk["path"]) == chunk["sha256"]

        imported = await import_telemetry(target.params, out, NullReporter())
        assert imported.tables_imported["session_events"] == 8
        assert await target.rows("SELECT count(*) AS c FROM session_events") == [{"c": 8}]
        assert await target.rows("SELECT count(*) AS c FROM session_stats_agg") == [{"c": 2}]
        validation = await validate_telemetry(target.params, None, out, NullReporter())
        assert validation.checksums_valid and validation.row_count_results["session_events"] == (8, 8)

        # The standalone validation command uses the same strict v3 contract as import.
        manifest["chunks"][1]["chunk_id"] = manifest["chunks"][0]["chunk_id"]
        (out / "telemetry_manifest.json").write_text(json.dumps(manifest, indent=2))
        with pytest.raises(ArtifactValidationError, match="duplicate"):
            await validate_telemetry(None, None, out, NullReporter())
