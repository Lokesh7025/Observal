# SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Telemetry store service tests (acceptance A-01 … A-11)."""

from __future__ import annotations

import asyncio
import hashlib
import re
from typing import TYPE_CHECKING

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from observal_shared.telemetry_keys import session_key
from observal_shared.telemetry_tables import EXTRA_ROW_LINE_OFFSET
from telemetry_store.db import SCHEMA_DIR, Database, SingleWriterViolationError
from telemetry_store.settings import TelemetrySettings

from .conftest import make_settings

if TYPE_CHECKING:
    from pathlib import Path


async def _q(store, sql, **params):
    r = await store.post("/v1/query", json={"sql": sql, "params": params})
    assert r.status_code == 200, r.text
    return r.json()["rows"]


# ── A-01 schema ──────────────────────────────────────────────────────


def test_schema_baseline_is_single_file_without_forbidden_ddl():
    files = sorted(SCHEMA_DIR.glob("*.sql"))
    assert [f.name for f in files] == ["001_baseline.sql"]
    text = files[0].read_text().upper()
    for forbidden in ("INSERT OR REPLACE", "PRIMARY KEY", "UNIQUE", "CREATE INDEX"):
        assert forbidden not in text


def test_schema_applies_once(tmp_path: Path):
    settings = make_settings(tmp_path)
    db = Database(settings)
    assert db.apply_schema() == ["001_baseline"]
    assert db.apply_schema() == []
    assert db.schema_version() == "001_baseline"
    counts = db.table_counts()
    assert "session_events" in counts and counts["schema_migrations"] == 1
    db.close()


def test_single_writer_lock(tmp_path: Path):
    settings = make_settings(tmp_path)
    first = Database(settings)
    first.open()
    second = Database(settings)
    with pytest.raises(SingleWriterViolationError):
        second.open()
    first.close()
    second.open()
    second.close()


def test_multi_worker_flag_is_rejected(monkeypatch):
    from telemetry_store.__main__ import _reject_multi_worker

    with pytest.raises(SystemExit):
        _reject_multi_worker(["--workers", "2"])
    monkeypatch.setenv("WEB_CONCURRENCY", "3")
    with pytest.raises(SystemExit):
        _reject_multi_worker([])
    monkeypatch.setenv("WEB_CONCURRENCY", "1")
    _reject_multi_worker([])


def test_settings_require_token(monkeypatch):
    monkeypatch.delenv("TELEMETRY_TOKEN", raising=False)
    monkeypatch.delenv("TELEMETRY_TOKEN_FILE", raising=False)
    with pytest.raises(ValueError):
        TelemetrySettings.from_env().validate()


# ── A-06 auth ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_auth_required(store):
    for path, method, body in (
        ("/v1/query", "post", {"sql": "SELECT 1"}),
        ("/v1/stats", "get", None),
        ("/v1/write/append", "post", {"table": "audit_log", "rows": []}),
        ("/metrics", "get", None),
    ):
        kwargs = {"json": body} if body is not None else {}
        r = await getattr(store, method)(path, headers={"Authorization": "Bearer wrong"}, **kwargs)
        assert r.status_code == 401, path
    r = await store.get("/v1/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


# ── A-03 read-only enforcement ───────────────────────────────────────


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO audit_log (actor_id) VALUES ('x')",
        "DELETE FROM session_events",
        "CREATE TABLE t (a INT)",
        "ATTACH ':memory:' AS other",
        "COPY audit_log TO '/tmp/x.parquet'",
        "SET memory_limit='1MB'",
        "SELECT 1; SELECT 2",
        "SELECT * FROM read_parquet('/etc/passwd')",
        "INSTALL httpfs",
    ],
)
@pytest.mark.asyncio
async def test_query_rejects_non_read_only(store, sql):
    r = await store.post("/v1/query", json={"sql": sql})
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "query_rejected"


@pytest.mark.asyncio
async def test_query_params_and_timestamp_format(store):
    rows = await _q(
        store, "SELECT $a + $b AS s, TIMESTAMP '2026-05-01 10:00:00.123' AS ts, $name AS n", a=1, b=2, name="x"
    )
    assert rows == [{"s": 3, "ts": "2026-05-01 10:00:00.123", "n": "x"}]


@pytest.mark.asyncio
async def test_query_bad_sql_is_400_not_empty(store):
    r = await store.post("/v1/query", json={"sql": "SELECT * FROM no_such_table"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "query_error"


# ── A-04 timeout ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_query_timeout_interrupts_and_connection_stays_usable(store):
    slow = "SELECT count(*) FROM range(2000000000) a, range(1000) b"
    t0 = asyncio.get_running_loop().time()
    r = await store.post("/v1/query", json={"sql": slow, "timeout_ms": 200})
    elapsed = asyncio.get_running_loop().time() - t0
    assert r.status_code == 504, r.text
    assert r.json()["error"]["code"] == "query_timeout"
    assert elapsed < 5
    assert await _q(store, "SELECT 1 AS one") == [{"one": 1}]


@pytest.mark.asyncio
async def test_result_too_large(store):
    r = await store.post("/v1/query", json={"sql": "SELECT * FROM range(20000)"})
    assert r.status_code == 413


# ── A-05 backpressure ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_backpressure_returns_429_not_hang(store):
    slow = "SELECT count(*) FROM range(300000000) a, range(1000) b"
    tasks = [store.post("/v1/query", json={"sql": slow, "timeout_ms": 400}) for _ in range(12)]
    results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=60)
    codes = sorted(r.status_code for r in results)
    assert 429 in codes
    assert set(codes) <= {429, 504}
    # Interrupted workers unwind asynchronously; wait for the pool to drain, then prove it is usable.
    for _ in range(600):
        health = (await store.get("/v1/health")).json()
        if health["read_pending"] == 0 and health["read_active"] == 0:
            break
        await asyncio.sleep(0.1)
    else:
        raise AssertionError(f"reader pool did not drain: {health}")
    assert await _q(store, "SELECT 1 AS one") == [{"one": 1}]


# ── A-02 replace semantics ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_replace_latest_wins_and_sentinel_preserved(store, event_row):
    first = [event_row("s1", i) for i in range(3)]
    r = await store.post("/v1/write/replace", json={"table": "session_events", "rows": first})
    assert r.status_code == 200 and r.json() == {"rows_written": 3, "rows_replaced": 0}

    again = [event_row("s1", 1, content_preview="changed")]
    r = await store.post("/v1/write/replace", json={"table": "session_events", "rows": again})
    assert r.json() == {"rows_written": 1, "rows_replaced": 1}

    credits = [event_row("s1", EXTRA_ROW_LINE_OFFSET, is_source_record=0, event_type="kiro_credits", credits=4.5)]
    r = await store.post("/v1/write/replace", json={"table": "session_events", "rows": credits})
    assert r.status_code == 200

    rows = await _q(
        store, "SELECT line_offset, content_preview, is_source_record FROM session_events ORDER BY line_offset"
    )
    assert [r_["line_offset"] for r_ in rows] == [0, 1, 2, EXTRA_ROW_LINE_OFFSET]
    assert rows[1]["content_preview"] == "changed"
    assert rows[3]["is_source_record"] is False

    # Duplicate identities are compared after type coercion and retain the
    # final payload in caller order.
    duplicate_first = event_row("dupe", 7, content_preview="first")
    duplicate_last = event_row("dupe", 7, content_preview="last")
    duplicate_last["line_offset"] = "7"
    r = await store.post(
        "/v1/write/replace",
        json={"table": "session_events", "rows": [duplicate_first, duplicate_last]},
    )
    assert r.status_code == 200 and r.json() == {"rows_written": 1, "rows_replaced": 0}
    assert await _q(
        store,
        "SELECT content_preview FROM session_events WHERE session_id = 'dupe' AND line_offset = 7",
    ) == [{"content_preview": "last"}]

    # Replaying an overlapping duplicate batch still leaves one canonical row.
    r = await store.post(
        "/v1/write/replace",
        json={
            "table": "session_events",
            "rows": [
                event_row("dupe", 7, content_preview="replay-first"),
                event_row("dupe", 7, content_preview="replay-last"),
            ],
        },
    )
    assert r.status_code == 200 and r.json() == {"rows_written": 1, "rows_replaced": 1}
    assert await _q(
        store,
        "SELECT count(*) AS c, max(content_preview) AS content FROM session_events "
        "WHERE session_id = 'dupe' AND line_offset = 7",
    ) == [{"c": 1, "content": "replay-last"}]

    sentinel_rows = [
        event_row("sentinel", EXTRA_ROW_LINE_OFFSET, is_source_record=0, content_preview="old"),
        event_row("sentinel", EXTRA_ROW_LINE_OFFSET, is_source_record=0, content_preview="new"),
    ]
    r = await store.post("/v1/write/replace", json={"table": "session_events", "rows": sentinel_rows})
    assert r.status_code == 200 and r.json()["rows_written"] == 1
    assert await _q(
        store,
        "SELECT count(*) AS c, max(content_preview) AS content FROM session_events WHERE session_id = 'sentinel'",
    ) == [{"c": 1, "content": "new"}]

    key = session_key("default", "u1", "claude-code", "s1")
    keys = await _q(store, "SELECT DISTINCT session_key AS k FROM session_events WHERE session_id = 's1'")
    assert keys == [{"k": key}]


@pytest.mark.asyncio
async def test_overlapping_batch_replay_is_stable_during_reads(store, event_row):
    rows = [event_row("pressure", offset) for offset in range(200)]

    async def replay() -> None:
        for generation in range(3):
            batch = [{**row, "content_preview": f"generation-{generation}"} for row in rows]
            response = await store.post(
                "/v1/write/replace",
                json={"table": "session_events", "rows": [*batch, *batch]},
            )
            assert response.status_code == 200, response.text
            assert response.json()["rows_written"] == len(rows)

    async def read() -> None:
        for _ in range(10):
            result = await _q(
                store,
                "SELECT count(*) AS c, max(line_offset) AS max_offset FROM session_events "
                "WHERE session_id = 'pressure'",
            )
            assert result[0]["c"] in {0, len(rows)}

    await asyncio.gather(replay(), *(read() for _ in range(3)))
    assert await _q(
        store,
        "SELECT count(*) AS c, count(DISTINCT line_offset) AS distinct_offsets, "
        "min(content_preview) AS content FROM session_events WHERE session_id = 'pressure'",
    ) == [{"c": len(rows), "distinct_offsets": len(rows), "content": "generation-2"}]


@pytest.mark.asyncio
async def test_write_rejects_unknown_table_and_columns(store, event_row):
    r = await store.post("/v1/write/replace", json={"table": "nope", "rows": [{}]})
    assert r.status_code in (422, 500)
    r = await store.post("/v1/write/replace", json={"table": "session_events", "rows": [event_row("s", 0, bogus=1)]})
    assert r.status_code == 422 and r.json()["error"]["code"] == "write_rejected"
    r = await store.post("/v1/write/append", json={"table": "session_events", "rows": [event_row("s", 0)]})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_session_batch_is_atomic_and_computes_summary_and_checkpoint(store, event_row):
    identity = {"project_id": "default", "user_id": "u1", "harness": "claude-code", "session_id": "s1"}
    events = [event_row("s1", i) for i in range(5)]
    r = await store.post(
        "/v1/write/session-batch",
        json={"events": events, "refresh_summary": True, "advance_checkpoint": identity},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["events_written"] == 5 and body["summaries_refreshed"] == 1
    assert body["checkpoint"] == {"acknowledged_line": 4, "acknowledged_offset": 500}

    summary = await _q(store, "SELECT * FROM session_stats_agg")
    assert len(summary) == 1
    s = summary[0]
    assert s["event_count"] == 5 and s["prompt_count"] == 3 and s["tool_call_count"] == 2
    assert s["input_tokens"] == 50 and s["model"] == "claude"
    assert s["first_event_time"] == "2026-05-01 10:00:00.000"
    assert s["last_event_time"] == "2026-05-01 10:00:04.000"

    # Gap: offsets 7,8 arrive without 5,6 → checkpoint stays at 4.
    r = await store.post(
        "/v1/write/session-batch",
        json={"events": [event_row("s1", 7), event_row("s1", 8)], "advance_checkpoint": identity},
    )
    assert r.json()["checkpoint"]["acknowledged_line"] == 4
    r = await store.post(
        "/v1/write/session-batch",
        json={"events": [event_row("s1", 5), event_row("s1", 6)], "advance_checkpoint": identity},
    )
    assert r.json()["checkpoint"] == {"acknowledged_line": 8, "acknowledged_offset": 900}

    # Atomicity: an invalid row in the batch leaves nothing behind.
    before = await _q(store, "SELECT count(*) AS c FROM session_events")
    bad = [event_row("s1", 9), event_row("s1", 10, bogus_column=1)]
    r = await store.post("/v1/write/session-batch", json={"events": bad, "advance_checkpoint": identity})
    assert r.status_code == 422
    after = await _q(store, "SELECT count(*) AS c FROM session_events")
    assert before == after


@pytest.mark.asyncio
async def test_checkpoint_upsert_and_repair_rewind(store, event_row):
    identity = {"project_id": "default", "user_id": "u1", "harness": "claude-code", "session_id": "s1"}
    await store.post(
        "/v1/write/session-batch",
        json={"events": [event_row("s1", i) for i in range(3)], "advance_checkpoint": identity},
    )
    r = await store.post("/v1/write/checkpoint", json={**identity, "acknowledged_line": 0, "acknowledged_offset": 100})
    assert r.status_code == 200
    rows = await _q(store, "SELECT acknowledged_line, acknowledged_offset FROM session_checkpoints")
    assert rows == [{"acknowledged_line": 0, "acknowledged_offset": 100}]


@pytest.mark.asyncio
async def test_append_tables(store):
    audit = [
        {
            "event_id": "11111111-1111-1111-1111-111111111111",
            "timestamp": "2026-05-01 10:00:00.000",
            "actor_id": "a",
            "action": "login",
        },
        {
            "event_id": "22222222-2222-2222-2222-222222222222",
            "timestamp": "2026-05-01T10:00:01Z",
            "actor_id": "b",
            "action": "logout",
        },
    ]
    r = await store.post("/v1/write/append", json={"table": "audit_log", "rows": audit})
    assert r.status_code == 200 and r.json() == {"rows_written": 2}
    rows = await _q(store, "SELECT event_id, timestamp, action, sensitivity FROM audit_log ORDER BY timestamp")
    assert rows[0]["event_id"] == "11111111-1111-1111-1111-111111111111"
    assert rows[1]["timestamp"] == "2026-05-01 10:00:01.000"
    assert rows[0]["sensitivity"] == "standard"


@pytest.mark.asyncio
async def test_layer_snapshot_replace(store):
    row = {
        "hash": "abc",
        "project_id": "default",
        "user_id": "u1",
        "harness": "pi",
        "content": "{}",
        "file_count": 1,
        "total_size": 2,
    }
    await store.post("/v1/write/replace", json={"table": "layer_snapshots", "rows": [row]})
    r = await store.post(
        "/v1/write/replace", json={"table": "layer_snapshots", "rows": [{**row, "content": '{"v":2}'}]}
    )
    assert r.json() == {"rows_written": 1, "rows_replaced": 1}
    rows = await _q(store, "SELECT content FROM layer_snapshots")
    assert rows == [{"content": '{"v":2}'}]

    r = await store.post(
        "/v1/write/replace",
        json={
            "table": "layer_snapshots",
            "rows": [{**row, "content": '{"v":3}'}, {**row, "content": '{"v":4}'}],
        },
    )
    assert r.json() == {"rows_written": 1, "rows_replaced": 1}
    assert await _q(store, "SELECT count(*) AS c, max(content) AS content FROM layer_snapshots") == [
        {"c": 1, "content": '{"v":4}'}
    ]


# ── A-12 retention primitives ────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_expire_and_orphans(store, event_row):
    old = [event_row("old", i, timestamp="2025-01-01 00:00:00.000") for i in range(3)]
    new = [event_row("new", i, timestamp="2026-05-01 00:00:00.000") for i in range(2)]
    for sid, rows in (("old", old), ("new", new)):
        identity = {"project_id": "default", "user_id": "u1", "harness": "claude-code", "session_id": sid}
        await store.post("/v1/write/session-batch", json={"events": rows, "advance_checkpoint": identity})

    r = await store.post("/v1/write/expire-raw-lines", json={"before": "2026-01-01 00:00:00"})
    assert r.json() == {"rows_expired": 3}
    rows = await _q(store, "SELECT raw_line, raw_line_truncated FROM session_events WHERE session_id='old' LIMIT 1")
    assert rows == [{"raw_line": "", "raw_line_truncated": 2}]

    r = await store.post(
        "/v1/write/delete",
        json={"table": "session_events", "where": {"timestamp_lt": "2026-01-01 00:00:00", "project_id": "default"}},
    )
    assert r.json() == {"rows_deleted": 3}
    r = await store.post("/v1/write/delete-orphan-summaries", json={"project_id": "default"})
    assert r.json() == {"rows_deleted": 2}  # one summary + one checkpoint
    assert await _q(store, "SELECT count(*) AS c FROM session_stats_agg") == [{"c": 1}]

    r = await store.post("/v1/write/delete", json={"table": "session_events", "where": {"raw": "1=1"}})
    assert r.status_code == 422
    r = await store.post("/v1/write/delete", json={"table": "session_events", "where": {}})
    assert r.status_code == 422


# ── A-08 / A-09 import ledger ────────────────────────────────────────


def _write_parquet(path: Path, rows: list[dict]) -> str:
    table = pa.Table.from_pylist(rows)
    pq.write_table(table, path)
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def _import_chunk(store, path: Path, **form):
    """POST a parquet chunk the way the migration client does: always as an upload."""
    with path.open("rb") as fh:
        return await store.post(
            "/v1/import/chunk", data=form, files={"file": (path.name, fh, "application/octet-stream")}
        )


@pytest.mark.asyncio
async def test_import_chunk_idempotent_and_checksummed(store, event_row, tmp_path: Path):
    rows = []
    for i in range(4):
        row = event_row("imp", i, timestamp="2026-03-01 00:00:00.000")
        row["ingested_at"] = "2026-03-01 00:00:01.000"
        rows.append(row)
    path = tmp_path / "chunk.parquet"
    sha = _write_parquet(path, rows)
    form = {"migration_id": "m1", "chunk_id": "c1", "table": "session_events", "sha256": sha}
    r = await _import_chunk(store, path, **form)
    assert r.status_code == 200, r.text
    assert r.json()["rows_written"] == 4 and r.json()["skipped"] is False

    r = await _import_chunk(store, path, **form)
    assert r.json()["skipped"] is True
    assert await _q(store, "SELECT count(*) AS c FROM session_events") == [{"c": 4}]
    assert await _q(store, "SELECT count(*) AS c FROM telemetry_import_ledger") == [{"c": 1}]

    r = await _import_chunk(store, path, **{**form, "chunk_id": "c2", "sha256": "0" * 64})
    assert r.status_code == 422 and "checksum" in r.json()["error"]["message"]
    assert await _q(store, "SELECT count(*) AS c FROM telemetry_import_ledger") == [{"c": 1}]

    # Same keys under a new chunk id → replaced, not duplicated
    r = await _import_chunk(store, path, **{**form, "chunk_id": "c3"})
    assert r.status_code == 200 and r.json()["rows_written"] == 4
    assert await _q(store, "SELECT count(*) AS c FROM session_events") == [{"c": 4}]

    # A caller-supplied local path is not accepted; chunks must be uploaded.
    r = await store.post("/v1/import/chunk", data={**form, "chunk_id": "c4", "path": str(path)})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_import_empty_chunk_is_recorded_noop(store, event_row, tmp_path: Path):
    """A zero-row chunk (empty source table) imports cleanly and lands in the ledger."""
    schema = pa.Table.from_pylist([event_row("x", 0)]).schema
    path = tmp_path / "empty.parquet"
    pq.write_table(pa.Table.from_pylist([], schema=schema), path)
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    for table in ("session_events", "layer_snapshots", "audit_log"):
        r = await _import_chunk(store, path, migration_id="m", chunk_id=f"e-{table}", table=table, sha256=sha)
        assert r.status_code == 200, (table, r.text)
        assert r.json() == {"skipped": False, "rows_written": 0, "rows_replaced": 0, "row_count": 0}
    assert await _q(store, "SELECT count(*) AS c FROM telemetry_import_ledger") == [{"c": 3}]
    r = await _import_chunk(store, path, migration_id="m", chunk_id="e-audit_log", table="audit_log", sha256=sha)
    assert r.json()["skipped"] is True


@pytest.mark.asyncio
async def test_import_never_overwrites_live_rows(store, event_row, tmp_path: Path):
    identity = {"project_id": "default", "user_id": "u1", "harness": "claude-code", "session_id": "live"}
    live = [event_row("live", 0, content_preview="LIVE")]
    await store.post("/v1/write/session-batch", json={"events": live, "advance_checkpoint": identity})

    old = event_row("live", 0, content_preview="OLD", timestamp="2026-01-01 00:00:00.000")
    old["ingested_at"] = "2026-01-01 00:00:00.000"
    other = event_row("live", 1, content_preview="OLD1", timestamp="2026-01-01 00:00:00.000")
    other["ingested_at"] = "2026-01-01 00:00:00.000"
    path = tmp_path / "old.parquet"
    sha = _write_parquet(path, [old, other])
    r = await _import_chunk(store, path, migration_id="m", chunk_id="c", table="session_events", sha256=sha)
    assert r.status_code == 200, r.text
    rows = await _q(store, "SELECT line_offset, content_preview FROM session_events ORDER BY line_offset")
    assert rows == [{"line_offset": 0, "content_preview": "LIVE"}, {"line_offset": 1, "content_preview": "OLD1"}]


@pytest.mark.asyncio
async def test_import_append_table_dedupes_on_id(store, tmp_path: Path):
    rows = [{"event_id": "33333333-3333-3333-3333-333333333333", "timestamp": "2026-01-01 00:00:00.000", "action": "x"}]
    path = tmp_path / "audit.parquet"
    sha = _write_parquet(path, rows)
    form = {"migration_id": "m", "chunk_id": "a1", "table": "audit_log", "sha256": sha}
    assert (await _import_chunk(store, path, **form)).status_code == 200
    sha2 = _write_parquet(tmp_path / "audit2.parquet", rows)
    r = await _import_chunk(store, tmp_path / "audit2.parquet", **{**form, "chunk_id": "a2", "sha256": sha2})
    assert r.status_code == 200
    assert await _q(store, "SELECT count(*) AS c FROM audit_log") == [{"c": 1}]


@pytest.mark.asyncio
async def test_import_rejects_derived_tables(store, tmp_path: Path):
    path = tmp_path / "x.parquet"
    sha = _write_parquet(path, [{"session_id": "s"}])
    r = await _import_chunk(store, path, migration_id="m", chunk_id="d", table="session_stats_agg", sha256=sha)
    assert r.status_code == 422


# ── A-10 rebuild ─────────────────────────────────────────────────────


async def _wait_job(store, job_id: str):
    for _ in range(200):
        r = await store.get(f"/v1/jobs/{job_id}")
        body = r.json()
        if body["state"] in ("done", "failed"):
            return body
        await asyncio.sleep(0.05)
    raise AssertionError("job did not finish")


@pytest.mark.asyncio
async def test_rebuild_matches_live_summary_and_never_regresses_checkpoint(store, event_row):
    identity = {"project_id": "default", "user_id": "u1", "harness": "claude-code", "session_id": "r1"}
    await store.post(
        "/v1/write/session-batch",
        json={"events": [event_row("r1", i) for i in range(6)], "advance_checkpoint": identity},
    )
    live_summary = (await _q(store, "SELECT * EXCLUDE (summary_version, updated_at) FROM session_stats_agg"))[0]
    # Simulate a live checkpoint ahead of what events alone justify.
    await store.post("/v1/write/checkpoint", json={**identity, "acknowledged_line": 9, "acknowledged_offset": 1000})

    r = await store.post("/v1/rebuild/derived", json={"all": True, "migration_id": "m1"})
    assert r.status_code == 202
    job = await _wait_job(store, r.json()["id"])
    assert job["state"] == "done", job
    assert job["result"]["sessions"] == 1

    rebuilt = (await _q(store, "SELECT * EXCLUDE (summary_version, updated_at) FROM session_stats_agg"))[0]
    assert rebuilt == live_summary
    ckpt = await _q(store, "SELECT acknowledged_line, acknowledged_offset FROM session_checkpoints")
    assert ckpt == [{"acknowledged_line": 9, "acknowledged_offset": 1000}]
    stats = (await store.get("/v1/stats")).json()
    assert stats["backfill"]["phase"] == "done"


# ── A-11 backup / export ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_backup_while_writing(store, event_row, tmp_path: Path):
    identity = {"project_id": "default", "user_id": "u1", "harness": "claude-code", "session_id": "b1"}
    await store.post(
        "/v1/write/session-batch",
        json={"events": [event_row("b1", i) for i in range(10)], "advance_checkpoint": identity},
    )
    dest = tmp_path / "backup" / "telemetry.duckdb"
    r = await store.post("/v1/admin/backup", json={"dest_path": str(dest)})
    assert r.status_code == 202
    # concurrent write during backup
    await store.post(
        "/v1/write/session-batch",
        json={"events": [event_row("b2", 0)], "advance_checkpoint": {**identity, "session_id": "b2"}},
    )
    job = await _wait_job(store, r.json()["id"])
    assert job["state"] == "done", job
    assert job["result"]["bytes"] > 0 and re.fullmatch(r"[0-9a-f]{64}", job["result"]["sha256"])

    conn = duckdb.connect(str(dest), read_only=True)
    count = conn.execute("SELECT count(*) FROM session_events").fetchone()[0]
    conn.close()
    assert count in (10, 11)


@pytest.mark.asyncio
async def test_export_parquet_with_manifest(store, event_row, tmp_path: Path):
    identity = {"project_id": "default", "user_id": "u1", "harness": "claude-code", "session_id": "e1"}
    await store.post(
        "/v1/write/session-batch",
        json={"events": [event_row("e1", i) for i in range(7)], "advance_checkpoint": identity},
    )
    dest = tmp_path / "export"
    r = await store.post("/v1/export", json={"tables": ["session_events", "session_stats_agg"], "dest_dir": str(dest)})
    assert r.status_code == 202
    job = await _wait_job(store, r.json()["id"])
    assert job["state"] == "done", job
    manifest = (dest / "telemetry_manifest.json").read_text()
    assert '"session_events-00000"' in manifest
    files = list((dest / "session_events").glob("*.parquet"))
    assert len(files) == 1
    assert pq.read_table(files[0]).num_rows == 7
    # Empty tables are recorded in the manifest but produce no chunk files.
    r = await store.post("/v1/export", json={"tables": ["layer_snapshots"], "dest_dir": str(tmp_path / "empty")})
    empty_job = await _wait_job(store, r.json()["id"])
    assert empty_job["state"] == "done" and empty_job["result"]["chunks"] == 0
    assert empty_job["result"]["tables"] == {"layer_snapshots": {"row_count": 0}}
    assert not (tmp_path / "empty" / "layer_snapshots").exists()
    r = await store.post("/v1/export", json={"tables": ["nope"], "dest_dir": str(dest)})
    assert r.status_code == 422

    # Download endpoint serves files inside the job directory only.
    job_id = job["id"]
    r = await store.get(f"/v1/export/{job_id}/files/telemetry_manifest.json")
    assert r.status_code == 200 and r.content == manifest.encode()
    outside = tmp_path / "secret.txt"
    outside.write_text("nope")
    for name in ("../secret.txt", "..%2Fsecret.txt", "/etc/passwd", "session_events/../../secret.txt"):
        r = await store.get(f"/v1/export/{job_id}/files/{name}")
        assert r.status_code == 404, name
    assert (await store.get("/v1/export/nope/files/telemetry_manifest.json")).status_code == 404


@pytest.mark.asyncio
async def test_pause_resume(store, event_row):
    r = await store.post("/v1/admin/pause-writes")
    assert r.json() == {"writer_paused": True}
    r = await store.post("/v1/write/append", json={"table": "audit_log", "rows": [{"actor_id": "a"}]})
    assert r.status_code == 503 and r.json()["error"]["code"] == "writer_paused"
    assert (await store.get("/v1/health")).json()["writer_paused"] is True
    await store.post("/v1/admin/resume-writes")
    r = await store.post("/v1/write/append", json={"table": "audit_log", "rows": [{"actor_id": "a"}]})
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_stats_and_metrics(store):
    r = await store.get("/v1/stats")
    assert r.status_code == 200 and "session_events" in r.json()["tables"]
    r = await store.get("/metrics")
    assert r.status_code == 200 and b"telemetry_query_seconds" in r.content
    r = await store.post("/v1/admin/checkpoint")
    assert r.json() == {"ok": True}


@pytest.mark.asyncio
async def test_writes_leave_no_staging_objects_behind(store, event_row):
    """Registered Arrow views and temp tables are released after every transaction."""
    identity = {"project_id": "default", "user_id": "u1", "harness": "claude-code", "session_id": "st"}
    await store.post("/v1/write/session-batch", json={"events": [event_row("st", 0)], "advance_checkpoint": identity})
    r = await store.post(
        "/v1/write/append",
        json={
            "table": "audit_log",
            "rows": [
                {"event_id": "33333333-3333-3333-3333-333333333333", "timestamp": "2026-05-01T10:00:00Z", "action": "a"}
            ],
        },
    )
    assert r.status_code == 200, r.text
    r = await store.get("/v1/stats")
    tables = r.json()["tables"]
    assert not [t for t in tables if t.startswith("_")], tables
    assert tables["session_events"] == 1 and tables["audit_log"] == 1
    views = await _q(store, "SELECT view_name FROM duckdb_views() WHERE NOT internal")
    assert views == []


# ── init-container migrate ──────────────────────────────────────────────


def test_migrate_file_mode_applies_schema(tmp_path: Path, monkeypatch):
    from telemetry_store import migrate

    monkeypatch.delenv("TELEMETRY_URL", raising=False)
    monkeypatch.setenv("TELEMETRY_DB_PATH", str(tmp_path / "m.duckdb"))
    monkeypatch.setenv("TELEMETRY_TOKEN", "t")
    assert migrate.main() == 0
    assert migrate.main() == 0  # idempotent
    db = Database(TelemetrySettings.from_env())
    try:
        tables = {r[0] for r in db.open().cursor().execute("SHOW TABLES").fetchall()}
    finally:
        db.close()
    assert {"session_events", "session_stats_agg", "telemetry_import_ledger"} <= tables


def test_migrate_service_mode_never_opens_file(tmp_path: Path, monkeypatch):
    """With TELEMETRY_URL set the init container only probes the running service."""
    from telemetry_store import migrate

    calls: list[str] = []

    class _Resp:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {"schema_version": "001_baseline", "duckdb_version": "x"}

    class _Client:
        def __init__(self, *a, **kw): ...

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, headers=None):
            calls.append(url)
            assert headers == {"Authorization": "Bearer tok"}
            return _Resp()

    monkeypatch.setattr(migrate.httpx, "Client", _Client)
    monkeypatch.setenv("TELEMETRY_URL", "http://observal-telemetry:8125/")
    monkeypatch.setenv("TELEMETRY_TOKEN", "tok")
    monkeypatch.setenv("TELEMETRY_DB_PATH", str(tmp_path / "never.duckdb"))
    assert migrate.main() == 0
    assert calls == ["http://observal-telemetry:8125/v1/health"]
    assert not (tmp_path / "never.duckdb").exists()


def test_migrate_service_mode_fails_when_unreachable(tmp_path: Path, monkeypatch):
    from telemetry_store import migrate

    monkeypatch.setenv("TELEMETRY_URL", "http://127.0.0.1:1")  # nothing listens here
    monkeypatch.setenv("TELEMETRY_MIGRATE_WAIT_S", "0")
    monkeypatch.setenv("TELEMETRY_DB_PATH", str(tmp_path / "never.duckdb"))
    assert migrate.main() == 1
    assert not (tmp_path / "never.duckdb").exists()
